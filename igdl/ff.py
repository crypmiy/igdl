"""Pick a working ffmpeg / ffprobe for igdl — by testing, not guessing.

Some ffmpeg builds (e.g. trimmed hardware-oriented builds) lack the mp3 encoder,
the image2 muxer or the segment muxer igdl needs. `python -m igdl.ff` (run by
install.sh) tries each candidate with the exact operations igdl performs and
links the first one that passes into $IGDL_HOME/bin/. The system ffmpeg is never
modified; the bundled static ffmpeg (imageio-ffmpeg) is the fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

BASE = Path(os.environ.get("IGDL_HOME", Path(__file__).resolve().parents[1]))
BIN = BASE / "bin"

CHECKS: dict[str, list[str]] = {
    "mp3 (libmp3lame)": ["-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "libmp3lame",
                         "-b:a", "128k", "{d}/t.mp3"],
    "jpg (image2)": ["-f", "lavfi", "-i", "testsrc2=size=64x64:d=1", "-frames:v", "1",
                     "-update", "1", "-c:v", "mjpeg", "-f", "image2", "{d}/t.jpg"],
    "mp4 (h264+aac)": ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:d=2", "-f", "lavfi",
                       "-i", "sine=f=440:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-shortest", "-movflags", "+faststart", "{d}/t.mp4"],
    "potong (segment)": ["-i", "{d}/t.mp4", "-map", "0", "-c", "copy", "-f", "segment",
                         "-segment_time", "1", "-reset_timestamps", "1", "{d}/s%02d.mp4"],
}


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:160] if lines else "?"


def check(exe: str) -> dict[str, Optional[str]]:
    """{check: None if ok else short reason}. Runs in a throwaway directory."""
    out: dict[str, Optional[str]] = {}
    with tempfile.TemporaryDirectory(prefix="igdl_ff_") as d:
        for name, args in CHECKS.items():
            cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"] + [a.format(d=d) for a in args]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                ok = r.returncode == 0
                out[name] = None if ok else _last_line(r.stderr) or f"exit {r.returncode}"
            except (OSError, subprocess.TimeoutExpired) as e:
                out[name] = f"{type(e).__name__}: {e}"[:160]
    return out


def _probe_ok(probe: str) -> Optional[str]:
    ff = ffmpeg()
    with tempfile.TemporaryDirectory(prefix="igdl_fp_") as d:
        t = f"{d}/t.mp4"
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=64x64:rate=10:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", t],
                       capture_output=True, timeout=60)
        try:
            r = subprocess.run([probe, "-v", "error", "-print_format", "json", "-show_format",
                                "-show_streams", t], capture_output=True, text=True, timeout=30)
            j = json.loads(r.stdout or "{}")
            return None if float(j.get("format", {}).get("duration") or 0) > 1 else _last_line(r.stderr)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return f"{type(e).__name__}: {e}"[:160]


def candidates() -> list[tuple[str, str]]:
    seen, out = set(), []

    def add(label, path):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            real = os.path.realpath(path)
            if real not in seen and real != os.path.realpath(BIN / "ffmpeg"):
                seen.add(real)
                out.append((label, path))

    add("IGDL_FFMPEG", os.environ.get("IGDL_FFMPEG"))
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.realpath(d) != os.path.realpath(BIN):
            add("sistem", os.path.join(d, "ffmpeg"))
    try:
        import imageio_ffmpeg
        add("bawaan igdl (statis)", imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    return out


def _link(name: str, target: Optional[str]) -> None:
    BIN.mkdir(parents=True, exist_ok=True)
    link = BIN / name
    if link.is_symlink() or link.exists():
        link.unlink()
    if target:
        link.symlink_to(os.path.realpath(target))


def version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
        first = (r.stdout.splitlines() or ["?"])[0].replace("ffmpeg version ", "")
        return first.split(" Copyright")[0].split(" https://")[0][:40]
    except OSError:
        return "?"


def setup() -> int:
    chosen = None
    for label, exe in candidates():
        res = check(exe)
        bad = {k: v for k, v in res.items() if v}
        mark = "✓ semua lolos" if not bad else "✗ " + ", ".join(bad)
        print(f"  {label:<22} {version(exe):<44} {mark}")
        for k, v in bad.items():
            print(f"      {k}: {v}")
        if not bad and chosen is None:
            chosen = exe
    _link("ffmpeg", chosen)
    if not chosen:
        print("  ✗ tidak ada ffmpeg yang lolos — konversi mp3/jpg/potong video akan gagal")
        return 1
    print(f"  → dipakai: {os.path.realpath(chosen)}")
    probe_ok = None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "ffprobe")
        if os.path.isfile(p) and os.access(p, os.X_OK) and os.path.realpath(d) != os.path.realpath(BIN):
            if (why := _probe_ok(p)) is None:
                probe_ok = p
                break
            print(f"  ffprobe {p}: ✗ {why}")
    _link("ffprobe", probe_ok)
    print(f"  ffprobe: {probe_ok or 'tidak ada yang lolos → igdl membaca info video lewat ffmpeg'}")
    return 0


# ------------------------------------------------------------------ runtime lookups
def ffmpeg() -> str:
    p = BIN / "ffmpeg"
    return str(p) if p.exists() else (shutil.which("ffmpeg") or "ffmpeg")


def ffprobe() -> Optional[str]:
    p = BIN / "ffprobe"
    if p.exists():
        return str(p)
    return None if (BIN / "ffmpeg").exists() else shutil.which("ffprobe")


def location() -> Optional[str]:
    """For yt-dlp's ffmpeg_location (our tested binary), or None for its default."""
    return str(BIN / "ffmpeg") if (BIN / "ffmpeg").exists() else None


if __name__ == "__main__":
    sys.exit(setup())
