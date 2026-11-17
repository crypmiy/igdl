"""YouTube videos, Shorts and playlists via yt-dlp (+ Deno for YouTube's JS challenges).

Three ways to deliver a video (`mode`):
  full   best quality up to 1080p, mp4          (CLI default; bot: split into ≤47 MB parts)
  fit    the best quality whose size fits ≤47 MB (bot only, no re-encode)
  audio  mp3 128 kbps                             (split into parts if needed)

Everything lands in $IGDL_HOME/data/media/yt_<id>/ and is reused: `.done_<mode>`
records the finished files, so asking for the same video/mode again is instant.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .core import BASE, MEDIA_CACHE, IGError, _short
from .media import MB, probe

Progress = Callable[[str, Optional[float], str], None]

PART_LIMIT = 47 * MB            # every file sent to Telegram stays under this
MAX_HEIGHT = 1080
MP3_KBPS = 128
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HOST_RE = re.compile(r"(?:^|\.)(?:youtube\.com|youtube-nocookie\.com|youtu\.be)$", re.I)


def _noop(stage: str, pct: Optional[float], detail: str) -> None:
    pass


# ------------------------------------------------------------------ links
def parse(url: str) -> Optional[tuple[str, str]]:
    """('video', id) or ('playlist', list_id) for YouTube links, else None."""
    p = urlparse(url)
    if not HOST_RE.search(p.netloc):
        return None
    parts = [s for s in p.path.split("/") if s]
    q = parse_qs(p.query)
    if p.netloc.lower().endswith("youtu.be") and parts and ID_RE.match(parts[0]):
        return "video", parts[0]
    if parts and parts[0] == "watch" and ID_RE.match((q.get("v") or [""])[0]):
        return "video", q["v"][0]                      # watch?v=…&list=… → just the video
    if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v") and ID_RE.match(parts[1]):
        return "video", parts[1]
    if parts and parts[0] == "playlist" and (q.get("list") or [""])[0]:
        return "playlist", q["list"][0]
    return None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def cache_dir(vid: str) -> Path:
    return MEDIA_CACHE / f"yt_{vid}"


# ------------------------------------------------------------------ yt-dlp plumbing
class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _opts(**extra) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(),
         "socket_timeout": 20, "retries": 3, "fragment_retries": 5, "noplaylist": True}
    deno = BASE / "bin" / "deno"
    o["js_runtimes"] = {"deno": {"path": str(deno)}} if deno.is_file() else {"deno": {}}
    if (ck := os.environ.get("YT_COOKIES", "").strip()) and Path(ck).expanduser().is_file():
        o["cookiefile"] = str(Path(ck).expanduser())
    o.update(extra)
    return o


def _friendly(e: Exception) -> IGError:
    msg = str(e)
    low = msg.lower()
    if "confirm your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return IGError("Video ini dibatasi umur — butuh login YouTube (belum didukung).")
    if "not a bot" in low:
        return IGError("YouTube meminta verifikasi \"bukan bot\" untuk IP ini. Coba lagi nanti; "
                       "kalau terus terjadi, butuh cookies YouTube (YT_COOKIES).")
    if "private video" in low:
        return IGError("Video ini private.")
    if "members-only" in low or "join this channel" in low:
        return IGError("Video ini khusus member channel.")
    if "video unavailable" in low or "has been removed" in low or "does not exist" in low:
        return IGError("Video tidak tersedia (dihapus, diblokir di wilayah ini, atau link salah).")
    if "js runtime" in low or "javascript runtime" in low or "deno" in low:
        return IGError("yt-dlp butuh Deno untuk YouTube — jalankan ./install.sh lagi.")
    return IGError(f"Gagal membaca/mengunduh dari YouTube.\n\n• yt-dlp: {_short(msg)}")


# ------------------------------------------------------------------ info + plan
_FMT_KEYS = ("format_id", "ext", "vcodec", "acodec", "height", "tbr", "abr", "filesize",
             "filesize_approx", "protocol")


def info(vid: str, refresh: bool = False) -> dict:
    """Slim metadata (title, duration, formats), cached on disk."""
    d = cache_dir(vid)
    f = d / "info.json"
    if f.is_file() and not refresh:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_opts(skip_download=True)) as ydl:
            raw = ydl.extract_info(watch_url(vid), download=False)
    except Exception as e:
        raise _friendly(e) from None
    slim = {"id": raw.get("id") or vid, "title": raw.get("title") or vid,
            "uploader": raw.get("uploader") or raw.get("channel") or "",
            "duration": int(raw.get("duration") or 0),
            "is_live": bool(raw.get("is_live")) or raw.get("live_status") in ("is_live", "is_upcoming"),
            "formats": [{k: fm.get(k) for k in _FMT_KEYS} for fm in raw.get("formats") or []],
            "fetched": int(time.time())}
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    return slim


def _size(fm: dict, dur: int) -> float:
    return float(fm.get("filesize") or fm.get("filesize_approx")
                 or (fm.get("tbr") or 0) * 1000 / 8 * dur)


def _usable(fm: dict) -> bool:
    return (fm.get("protocol") or "").startswith(("http", "m3u8")) and \
        "storyboard" not in (fm.get("format_id") or "") and (fm.get("ext") or "") != "mhtml"


@dataclass
class Choice:
    fmt: str            # yt-dlp format string (explicit ids + fallback)
    height: int
    size: float         # estimated bytes


@dataclass
class Plan:
    title: str
    duration: int
    full: Optional[Choice]
    fit: Optional[Choice]
    audio_size: float
    choices: list = None            # best combo per height, highest first

    def full_within(self, max_parts: int) -> Optional[Choice]:
        """Highest quality whose split stays within max_parts (bot: no 47-part floods)."""
        for c in self.choices or []:
            if math.ceil(c.size / PART_LIMIT) <= max_parts:
                return c
        return None

    @staticmethod
    def parts(c: Optional[Choice]) -> int:
        return max(1, math.ceil(c.size / PART_LIMIT)) if c else 0

    @property
    def full_parts(self) -> int:
        return max(1, math.ceil(self.full.size / PART_LIMIT)) if self.full else 0

    @property
    def audio_parts(self) -> int:
        return max(1, math.ceil(self.audio_size / PART_LIMIT))


def plan(inf: dict, max_height: int = MAX_HEIGHT) -> Plan:
    """full = best ≤ max_height with the best audio; fit = the highest quality whose
    estimated size fits PART_LIMIT, trying smaller m4a audio before a lower resolution."""
    dur = int(inf.get("duration") or 0)
    fmts = [f for f in inf.get("formats") or [] if _usable(f)]
    audios = [f for f in fmts if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    rank = lambda f: ((f.get("ext") == "m4a"), f.get("abr") or f.get("tbr") or 0)   # noqa: E731
    audios.sort(key=rank, reverse=True)
    best_audio = audios[0] if audios else None
    m4a = [f for f in audios if f.get("ext") == "m4a" and (f.get("abr") or f.get("tbr") or 0) >= 40]
    by_h: dict[int, tuple] = {}
    for f in fmts:                       # per height prefer H.264 (plays everywhere), then bitrate
        if f.get("vcodec") in (None, "none") or not f.get("height") or f["height"] > max_height:
            continue
        key = ((f.get("vcodec") or "").startswith("avc1"), f.get("tbr") or 0)
        if f["height"] not in by_h or key > by_h[f["height"]][0]:
            by_h[f["height"]] = (key, f)

    def combo(v, a):
        h = v["height"]
        generic = f"bv*[height<={h}]+ba/b[height<={h}]"
        if v.get("acodec") not in (None, "none") or a is None:
            return Choice(f"{v['format_id']}/{generic}", h, _size(v, dur))
        return Choice(f"{v['format_id']}+{a['format_id']}/{generic}", h, _size(v, dur) + _size(a, dur))

    heights = sorted(by_h, reverse=True)
    choices = [combo(by_h[h][1], best_audio) for h in heights]
    full = choices[0] if choices else None
    fit = None
    for h in heights:
        v = by_h[h][1]
        for a in ([best_audio] + m4a[::-1] if best_audio else [None]):   # best, then smallest m4a
            c = combo(v, a)
            if 0 < c.size <= PART_LIMIT * 0.95:
                fit = c
                break
        if fit:
            break
    return Plan(inf.get("title") or inf.get("id", ""), dur, full, fit, dur * MP3_KBPS * 1000 / 8, choices)


# ------------------------------------------------------------------ splitting
def split(path: Path, limit: int = PART_LIMIT, cb: Progress = _noop) -> list[Path]:
    """Cut into ≤ limit parts by time, stream copy (seconds, no re-encode)."""
    if path.stat().st_size <= limit:
        return [path]
    dur = probe(path).duration or 1
    n = math.ceil(path.stat().st_size / (limit * 0.95))   # 5% headroom for keyframe snapping
    seg = dur / n + 0.5                               # equal parts; +0.5 s avoids a sliver at the end
    pat = path.with_name(f"{path.stem}_part%02d{path.suffix}")
    for attempt in range(4):
        for old in path.parent.glob(f"{path.stem}_part*{path.suffix}"):
            old.unlink()
        cb("potong", None, f"jadi ±{math.ceil(dur / seg)} bagian (tanpa encode ulang)")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
               "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", f"{seg:.2f}",
               "-reset_timestamps", "1"]
        if path.suffix == ".mp4":
            cmd += ["-segment_format_options", "movflags=+faststart"]
        r = subprocess.run(cmd + [str(pat)], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg gagal memotong: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        parts = sorted(path.parent.glob(f"{path.stem}_part*{path.suffix}"))
        biggest = max((p.stat().st_size for p in parts), default=0)
        if parts and biggest <= limit:
            return parts
        n += 1                                        # long keyframe gaps → one more piece
        seg = dur / n + 0.5
    raise RuntimeError("tidak bisa memotong video jadi bagian ≤ 47 MB")


# ------------------------------------------------------------------ download
def download(vid: str, mode: str, cb: Progress = _noop, *, split_parts: bool = False,
             max_height: int = MAX_HEIGHT) -> tuple[list[Path], str]:
    """Returns (files, 'cache'|'yt-dlp'). mode: full | fit | audio."""
    d = cache_dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    tag = mode if mode != "full" or max_height == MAX_HEIGHT else f"full{max_height}"
    marker = d / (f".done_{tag}" + ("_split" if split_parts else ""))
    if marker.is_file():
        files = [d / n for n in json.loads(marker.read_text())]
        if files and all(f.is_file() for f in files):
            cb("cache", 100.0, f"{len(files)} file dari disk")
            return files, "cache"

    base_marker = d / f".done_{tag}"
    main: Optional[Path] = None
    if base_marker.is_file():
        names = json.loads(base_marker.read_text())
        if names and (d / names[0]).is_file():
            main = d / names[0]
    if main is None:
        main = _fetch(vid, mode, tag, d, cb, max_height)
        base_marker.write_text(json.dumps([main.name]))
    files = split(main, PART_LIMIT, cb) if split_parts else [main]
    marker.write_text(json.dumps([f.name for f in files]))
    return files, "yt-dlp"


def _fetch(vid: str, mode: str, tag: str, d: Path, cb: Progress, max_height: int) -> Path:
    import yt_dlp
    inf = info(vid)
    if inf.get("is_live"):
        raise IGError("Ini siaran langsung / belum tayang — belum bisa diunduh.")
    p = plan(inf, max_height)
    streams = {"n": 0, "last": None}

    def hook(st):
        if st.get("status") != "downloading":
            return
        fn = st.get("filename")
        if fn != streams["last"]:
            streams["last"] = fn
            streams["n"] += 1
        tot = st.get("total_bytes") or st.get("total_bytes_estimate")
        done = st.get("downloaded_bytes") or 0
        pct = 100.0 * done / tot if tot else None
        what = "audio" if mode == "audio" else ("video" if streams["n"] == 1 else "audio")
        detail = f"{what} · {done / MB:.1f}/{(tot or 0) / MB:.0f} MB"
        if st.get("speed"):
            detail += f" · {st['speed'] / MB:.1f} MB/s"
        if st.get("eta") is not None:
            detail += f" · ETA {int(st['eta'])} dtk"
        cb("unduh YouTube", pct, detail)

    def pp_hook(st):
        if st.get("status") == "started":
            name = st.get("postprocessor") or ""
            cb("konversi mp3" if "ExtractAudio" in name else "menggabungkan video+audio", None, "ffmpeg")

    out = d / f"{tag}.%(ext)s"
    if mode == "audio":
        o = _opts(format="ba[ext=m4a]/ba/b", outtmpl=str(out),
                  postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": str(MP3_KBPS)}])
        final_ext = ".mp3"
    else:
        ch = p.fit if mode == "fit" else p.full
        if ch is None:
            raise IGError("Tidak ada format video yang muat ≤ 50 MB — pilih \"Kualitas penuh "
                          "(dibagi)\" atau \"Audio\"." if mode == "fit" else "Tidak ada format video.")
        o = _opts(format=ch.fmt, outtmpl=str(out), merge_output_format="mp4",
                  postprocessor_args={"merger": ["-movflags", "+faststart"]})
        final_ext = ".mp4"
    o["progress_hooks"] = [hook]
    o["postprocessor_hooks"] = [pp_hook]
    for old in d.glob(f"{tag}.*"):                    # leftovers of an interrupted run
        if old.suffix in (".part", ".ytdl") or ".f" in old.name:
            old.unlink(missing_ok=True)
    cb("unduh YouTube", None, "memulai")
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.download([watch_url(vid)])
    except Exception as e:
        raise _friendly(e) from None
    target = d / f"{tag}{final_ext}"
    if not target.is_file():
        cands = [f for f in d.glob(f"{tag}.*") if f.suffix in (".mp4", ".mkv", ".webm", ".mp3", ".m4a")]
        if not cands:
            raise IGError("yt-dlp selesai tapi file hasil tidak ditemukan.")
        shutil.move(str(cands[0]), target.with_suffix(cands[0].suffix))
        target = target.with_suffix(cands[0].suffix)
    return target


# ------------------------------------------------------------------ playlists
def playlist(list_id: str, limit: Optional[int] = None) -> tuple[str, list[tuple[str, str]]]:
    """(playlist title, [(video_id, title)]) without touching each video."""
    import yt_dlp
    o = _opts(extract_flat="in_playlist", noplaylist=False, skip_download=True)
    if limit:
        o["playlistend"] = limit
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            raw = ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)
    except Exception as e:
        raise _friendly(e) from None
    out = []
    for e in raw.get("entries") or []:
        vid = (e or {}).get("id")
        if vid and ID_RE.match(vid):
            out.append((vid, e.get("title") or vid))
    if not out:
        raise IGError("Playlist kosong atau private.")
    return raw.get("title") or list_id, out


def safe_name(title: str, vid: str, ext: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip()[:120] or vid
    return f"{t} [{vid}]{ext}"


def fmt_dur(sec: int) -> str:
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
