"""Instagram media fetcher (no login).

Chain: gallery-dl -> yt-dlp -> embed page.  Each stage stops at the first
one that produces files.  A progress callback receives (stage, pct, detail).
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

Progress = Callable[[str, Optional[float], str], None]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

IG_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/[^\s<>\"']+", re.I)
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".mp4", ".mov", ".webm", ".m4v"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

NET_HINTS = ("tunnel connection failed", "unable to connect", "name resolution",
             "name or service not known", "timed out", "network is unreachable",
             "connection refused", "connection reset", "no route to host",
             "proxyerror", "connectionerror")
LOGIN_HINTS = ("login", "log in", "401", "403", "429", "rate", "checkpoint",
               "redirect", "private", "not available", "unavailable")


def _noop(stage: str, pct: Optional[float], detail: str) -> None:
    pass


class IGError(Exception):
    """User-facing failure (message is shown as-is)."""


@dataclass
class Target:
    url: str          # canonical URL
    kind: str         # post | reel | story | highlight | profile | unknown
    code: str = ""    # shortcode for post/reel

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.code}" if self.code else self.url

    @property
    def needs_login(self) -> bool:
        return self.kind in {"story", "highlight", "profile"}


def find_urls(text: str) -> list[str]:
    seen, out = set(), []
    for u in IG_URL_RE.findall(text or ""):
        u = u.rstrip(").,;!>]")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _resolve_share(url: str) -> str:
    """instagram.com/share/... links are redirects; follow them (best effort)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception:
        return url


def classify(url: str) -> Target:
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    if parts and parts[0] == "share":
        resolved = _resolve_share(url)
        if resolved != url and "/share/" not in resolved:
            return classify(resolved)
        return Target(url, "unknown")
    if not parts:
        return Target(url, "unknown")
    if parts[0] == "stories":
        kind = "highlight" if len(parts) > 1 and parts[1] == "highlights" else "story"
        return Target(url, kind)
    # /p/CODE, /reel/CODE, /reels/CODE, /tv/CODE, and /<user>/p/CODE
    for i, seg in enumerate(parts[:3]):
        if seg in {"p", "reel", "reels", "tv"} and i + 1 < len(parts):
            code = parts[i + 1]
            kind = "post" if seg in {"p", "tv"} else "reel"
            base = "p" if kind == "post" else "reel"
            return Target(f"https://www.instagram.com/{base}/{code}/", kind, code)
    if len(parts) == 1 or parts[1] in {"reels", "tagged"}:
        return Target(url, "profile")
    return Target(url, "unknown")


def _media_files(d: Path) -> list[Path]:
    files = [f for f in d.iterdir()
             if f.is_file() and f.suffix.lower() in MEDIA_EXT and f.stat().st_size > 0]
    return sorted(files, key=lambda f: f.name)


def _cookies() -> Optional[str]:
    c = os.environ.get("IG_COOKIES", "").strip()
    return c if c and Path(c).expanduser().is_file() else None


# ---------------------------------------------------------------- gallery-dl
def _via_gallery_dl(t: Target, dest: Path, cb: Progress) -> list[Path]:
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(dest),
           "-f", "{num:>02}_{media_id}.{extension}",
           "--http-timeout", "20", "-R", "2", "--no-part",
           "-o", "extractor.instagram.videos=true"]
    if ck := _cookies():
        cmd += ["-C", str(Path(ck).expanduser())]
    cmd.append(t.url)
    cb("gallery-dl", None, "mengambil info post")
    with tempfile.TemporaryFile("w+") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True, bufsize=1)
        watchdog = threading.Timer(240, proc.kill)   # hard cap per post
        watchdog.start()
        n = 0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.strip() and not line.startswith("#"):
                    n += 1
                    cb("gallery-dl", None, f"{n} file terunduh")
            proc.wait()
        finally:
            watchdog.cancel()
        errf.seek(0)
        err = errf.read()
    files = _media_files(dest)
    if not files:
        raise RuntimeError(err.strip()[-400:] or f"gallery-dl exit {proc.returncode}")
    return files


# ---------------------------------------------------------------- yt-dlp
def _via_yt_dlp(t: Target, dest: Path, cb: Progress) -> list[Path]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (100.0 * done / tot) if tot else None
            spd = d.get("speed")
            eta = d.get("eta")
            detail = f"{done/1e6:.1f} MB"
            if spd:
                detail += f" @ {spd/1e6:.1f} MB/s"
            if eta is not None:
                detail += f", ETA {int(eta)} dtk"
            cb("yt-dlp", pct, detail)

    class _Quiet:
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): pass

    opts = {
        "logger": _Quiet(),
        "outtmpl": str(dest / "%(playlist_index|0)02d_%(id)s.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook],
        "retries": 2, "socket_timeout": 20,
        "http_headers": {"User-Agent": UA},
    }
    if ck := _cookies():
        opts["cookiefile"] = str(Path(ck).expanduser())
    cb("yt-dlp", None, "mencoba jalur cadangan")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([t.url])
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        raise RuntimeError(str(e)[-400:])
    files = _media_files(dest)
    if not files:
        raise RuntimeError("yt-dlp: tidak ada file")
    return files


# ---------------------------------------------------------------- embed page
def _http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _walk_shortcode_media(obj):
    if isinstance(obj, dict):
        if "shortcode_media" in obj and isinstance(obj["shortcode_media"], dict):
            return obj["shortcode_media"]
        for v in obj.values():
            if (r := _walk_shortcode_media(v)) is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            if (r := _walk_shortcode_media(v)) is not None:
                return r
    return None


def _embed_items(page: str) -> list[tuple[str, str]]:
    """Return [(url, ext)] parsed from an embed page."""
    items: list[tuple[str, str]] = []
    m = re.search(r'"contextJSON"\s*:\s*("(?:\\.|[^"\\])*")', page)
    if m:
        try:
            ctx = json.loads(json.loads(m.group(1)))
            media = _walk_shortcode_media(ctx)
            if media:
                edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
                nodes = [e.get("node", {}) for e in edges] or [media]
                for nd in nodes:
                    if nd.get("is_video") and nd.get("video_url"):
                        items.append((nd["video_url"], ".mp4"))
                    elif nd.get("display_url"):
                        items.append((nd["display_url"], ".jpg"))
        except Exception:
            items = []
    if not items:
        for src in re.findall(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', page):
            items.append((html.unescape(src), ".jpg"))
        for src in re.findall(r'<video[^>]*src="([^"]+)"', page):
            items.append((html.unescape(src), ".mp4"))
    return items


def _via_embed(t: Target, dest: Path, cb: Progress) -> list[Path]:
    if not t.code:
        raise RuntimeError("embed: butuh shortcode")
    cb("embed", None, "membaca halaman embed")
    base = "p" if t.kind == "post" else "reel"
    page = _http_get(f"https://www.instagram.com/{base}/{t.code}/embed/captioned/").decode(
        "utf-8", "replace")
    items = _embed_items(page)
    if not items:
        raise RuntimeError("embed: media tidak ditemukan")
    for i, (u, ext) in enumerate(items, 1):
        cb("embed", 100.0 * (i - 1) / len(items), f"file {i}/{len(items)}")
        (dest / f"{i:02d}_{t.code}{ext}").write_bytes(_http_get(u, timeout=60))
    return _media_files(dest)


# ---------------------------------------------------------------- public API
def precheck(t: Target) -> Optional[str]:
    """Reason this link can't be handled without login, or None."""
    if t.needs_login:
        return ("Story, highlight, dan halaman profil butuh login. "
                "Kirim link post (/p/…) atau reel (/reel/…).")
    if t.kind == "unknown":
        return "Link tidak dikenali sebagai post atau reel Instagram."
    return None


def download(t: Target, dest: Path, cb: Progress = _noop) -> tuple[list[Path], str]:
    """Download all media of a post/reel into dest. Returns (files, method)."""
    if reason := precheck(t):
        raise IGError(reason)
    dest.mkdir(parents=True, exist_ok=True)
    errors = []
    for name, fn in (("gallery-dl", _via_gallery_dl), ("yt-dlp", _via_yt_dlp),
                     ("embed", _via_embed)):
        for f in dest.iterdir():          # clean leftovers from a failed stage
            if f.is_file():
                f.unlink()
            elif f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
        try:
            files = fn(t, dest, cb)
            if files:
                return files, name
        except Exception as e:
            errors.append(f"{name}: {e}")
    blob = " ".join(errors).lower()
    if all(any(h in e.lower() for h in NET_HINTS) for e in errors):
        raise IGError("Jetson tidak bisa menghubungi Instagram (masalah jaringan/DNS). "
                      "Cek koneksi lalu coba lagi.\n\nDetail: " + errors[-1][:300])
    if any(h in blob for h in LOGIN_HINTS):
        raise IGError("Instagram menolak akses tanpa login (post private, dihapus, "
                      "atau sedang rate-limit). Coba lagi beberapa menit lagi.\n\n"
                      "Detail: " + errors[-1][:300])
    raise IGError("Gagal mengunduh.\n\nDetail: " + " | ".join(e[:200] for e in errors))


def is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXT
