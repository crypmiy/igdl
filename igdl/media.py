"""ffmpeg helpers: probe, fit a video under a byte limit, normalise photos."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

Progress = Callable[[str, Optional[float], str], None]

MB = 1024 * 1024
TG_VIDEO_LIMIT = 49 * MB      # Bot API upload cap is 50 MB; keep a margin
TG_PHOTO_LIMIT = 10 * MB      # sendPhoto cap
AUDIO_KBPS = 96


@dataclass
class Info:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(p: Path) -> Info:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, timeout=60, check=True).stdout
    j = json.loads(out)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", 0) or 0)
    if abs(rot) in (90, 270):
        w, h = h, w
    return Info(dur, w, h, a)


def _even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def _scale_for(info: Info, vkbps: float) -> Optional[str]:
    """Cap the shorter side according to the bitrate we can afford."""
    cap = 1080 if vkbps >= 2000 else 720 if vkbps >= 900 else 540 if vkbps >= 450 else 360
    short = min(info.width, info.height) or cap
    if short <= cap:
        return None
    f = cap / short
    return f"scale={_even(info.width * f)}:{_even(info.height * f)}"


def _encode(src: Path, dst: Path, info: Info, vkbps: int, cb: Progress, label: str) -> None:
    vf = _scale_for(info, vkbps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-i", str(src),
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-b:v", f"{vkbps}k", "-maxrate", f"{int(vkbps * 1.25)}k",
           "-bufsize", f"{int(vkbps * 2)}k", "-threads", "0"]
    if vf:
        cmd += ["-vf", vf]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", str(dst)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and info.duration > 0:
            try:
                t = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            cb("kompres", min(99.0, 100 * t / info.duration), label)
    rc = proc.wait()
    if rc != 0:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"ffmpeg gagal: {err.strip()[-300:]}")


def fit_video(src: Path, limit: int = TG_VIDEO_LIMIT, cb: Progress = lambda *a: None) -> Path:
    """Return src if already under limit, else a compressed copy under limit."""
    if src.stat().st_size <= limit:
        return src
    info = probe(src)
    if info.duration <= 0:
        raise RuntimeError("durasi video tidak terbaca")
    budget_kbps = (limit * 0.94 * 8 / 1000) / info.duration
    vkbps = int(budget_kbps - (AUDIO_KBPS if info.has_audio else 0))
    if vkbps < 120:
        raise RuntimeError(f"video terlalu panjang ({info.duration/60:.0f} menit) "
                           "untuk dimuat di bawah 50 MB")
    dst = src.with_name(src.stem + "_tg.mp4")
    for attempt in range(3):
        label = f"{src.stat().st_size/MB:.0f} MB → <{limit/MB:.0f} MB, {vkbps} kbps"
        if attempt:
            label += f" (ulang #{attempt})"
        _encode(src, dst, info, vkbps, cb, label)
        if dst.stat().st_size <= limit:
            cb("kompres", 100.0, f"{dst.stat().st_size/MB:.1f} MB")
            return dst
        vkbps = int(vkbps * 0.8 * limit / dst.stat().st_size)
    raise RuntimeError("kompresi tidak berhasil masuk batas 50 MB")


def normalise_photo(src: Path) -> Path:
    """Telegram-safe JPEG: convert webp/heic, shrink if > 10 MB."""
    ext = src.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png") and src.stat().st_size <= TG_PHOTO_LIMIT:
        return src
    dst = src.with_name(src.stem + "_tg.jpg")
    vf = "scale='min(2560,iw)':-2" if src.stat().st_size > TG_PHOTO_LIMIT else "null"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src), "-vf", vf, "-q:v", "3", "-frames:v", "1", str(dst)],
                   check=True, timeout=60)
    return dst


def video_meta(p: Path) -> dict:
    try:
        i = probe(p)
        return {"duration": int(round(i.duration)), "width": i.width, "height": i.height}
    except Exception:
        return {}


def cleanup(paths) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass
