"""Threads (threads.com / threads.net) posts, no login.

The logged-out post page embeds the same media JSON Instagram uses
(image_versions2 / video_versions / carousel_media) inside <script data-sjs>
blobs, grouped as `thread_items`.  We take the linked post plus the author's
own continuation of the thread; replies by other people and quoted/reposted
posts are left out.
"""
from __future__ import annotations

import html
import json
import re
import threading
from pathlib import Path
from typing import Callable, Optional

from .graphql import fetch_files, media_items

Progress = Callable[[str, Optional[float], str], None]

ROOT = "https://www.threads.com"
HOST_RE = re.compile(r"(?:^|\.)threads\.(?:com|net)$", re.I)
STAGE = "threads"

_lock = threading.Lock()
_sess: dict = {}


def session():
    with _lock:
        if "s" not in _sess:
            try:
                from curl_cffi import requests as cr
            except ImportError as e:
                raise RuntimeError("curl_cffi belum terpasang (jalankan ./install.sh)") from e
            _sess["s"] = cr.Session(impersonate="chrome")
        return _sess["s"]


# ------------------------------------------------------------------ parsing
def _sjs_blobs(page: str, code: str) -> list:
    out = []
    for raw in re.findall(r"<script\b[^>]*\bdata-sjs\b[^>]*>(.*?)</script>", page, re.S):
        if "thread_items" not in raw and code not in raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _author(post: dict) -> str:
    return ((post.get("user") or {}).get("username") or "").lower()


def _is_post(d: dict) -> bool:
    return isinstance(d.get("code"), str) and isinstance(d.get("user"), dict) and \
        any(k in d for k in ("image_versions2", "carousel_media", "video_versions", "caption"))


def _thread_posts(el) -> Optional[list[dict]]:
    """Posts of one thread element ({"node": {"thread_items": [...]}} or {"thread_items": ...})."""
    if not isinstance(el, dict):
        return None
    items = (el.get("node") if isinstance(el.get("node"), dict) else el).get("thread_items")
    if not isinstance(items, list):
        return None
    posts = [it.get("post") for it in items if isinstance(it, dict)]
    return [p for p in posts if isinstance(p, dict) and _is_post(p)] or None


def _groups(obj) -> tuple[list[list[list[dict]]], dict[str, dict]]:
    """Document-ordered groups of sibling threads (e.g. one `edges` list = the
    post's own thread followed by its reply threads), plus every post by code."""
    groups: list[list[list[dict]]] = []
    loose: dict[str, dict] = {}

    def rec(o):
        if isinstance(o, dict):
            if _is_post(o):
                loose.setdefault(o["code"], o)
            if (own := _thread_posts(o)) is not None and not isinstance(o.get("node"), dict):
                groups.append([own])
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            sib = [tp for tp in (_thread_posts(e) for e in o) if tp]
            if sib and any(isinstance(e, dict) and isinstance(e.get("node"), dict) for e in o):
                groups.append(sib)
            for v in o:
                rec(v)

    rec(obj)
    return groups, loose


def select_posts(blobs: list, code: str) -> list[dict]:
    """Linked post first, then the author's own continuation, in thread order:
    later posts by the author in the same thread and — when the post sits in the
    main thread — reply threads in the same group that start with the author
    (only their leading author posts). Parents, other people's replies, quoted posts and
    'related posts' elsewhere on the page are not taken."""
    groups: list[list[list[dict]]] = []
    loose: dict[str, dict] = {}
    for b in blobs:
        g, lo = _groups(b)
        groups += g
        for k, v in lo.items():
            loose.setdefault(k, v)

    for group in groups:
        for ti, thread in enumerate(group):
            idx = next((i for i, p in enumerate(thread) if p["code"] == code), None)
            if idx is None:
                continue
            author = _author(thread[idx])
            chain = [thread[idx]]
            for p in thread[idx + 1:]:
                if _author(p) != author:
                    break
                chain.append(p)
            for later in (group[1:] if ti == 0 else []):   # only under the main thread
                if _author(later[0]) != author:
                    continue
                for p in later:
                    if _author(p) != author:
                        break
                    chain.append(p)
            seen, out = set(), []
            for p in chain:
                if p["code"] not in seen:
                    seen.add(p["code"])
                    out.append(p)
            return out
    target = loose.get(code)
    return [target] if target else []


def caption_text(posts: list[dict]) -> str:
    parts = []
    for p in posts:
        txt = ((p.get("caption") or {}).get("text") or "").strip()
        if txt:
            parts.append(txt)
    author = _author(posts[0]) if posts else ""
    body = "\n\n".join(parts)
    return f"@{author}\n{body}".strip() if author else body


def _html_media(page: str) -> list[tuple[str, str]]:
    out = []
    for src in re.findall(r'<video\b[^>]*\bsrc="([^"]+)"', page):
        out.append((html.unescape(src), ".mp4"))
    for src in re.findall(r'<img\b[^>]*\bsrc="(https://[^"]*(?:cdninstagram|fbcdn)[^"]+)"', page):
        if "/s150x150/" not in src and "profile" not in src:
            out.append((html.unescape(src), ".jpg"))
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ download
def _fetch_page(url: str) -> str:
    r = session().get(url, timeout=25)
    if "/login" in str(r.url):
        raise RuntimeError("dialihkan ke halaman login")
    if r.status_code == 404:
        raise RuntimeError("post tidak ditemukan (dihapus atau private)")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def _save(posts: list[dict], items, dest: Path, code: str, cb: Progress) -> list[Path]:
    if not items:
        raise RuntimeError("post ini hanya teks, tidak ada foto/video")
    if posts and (txt := caption_text(posts)):
        (dest / "caption.txt").write_text(txt, encoding="utf-8")
    return fetch_files(session(), items, dest, code, cb, STAGE, ROOT + "/")


def download_page(url: str, code: str, dest: Path, cb: Progress) -> list[Path]:
    cb(STAGE, None, "membaca post Threads (tanpa login)")
    posts = select_posts(_sjs_blobs(_fetch_page(url), code), code)
    if not posts:
        raise RuntimeError("data post tidak ditemukan di halaman")
    items = [it for p in posts for it in media_items(p)]
    if len(posts) > 1:
        cb(STAGE, None, f"{len(posts)} post dalam utas, {len(items)} media")
    return _save(posts, items, dest, code, cb)


def download_embed(url: str, code: str, dest: Path, cb: Progress) -> list[Path]:
    cb("threads-embed", None, "membaca halaman embed")
    base = url.split("?")[0].rstrip("/")
    page = _fetch_page(base + "/embed")
    posts = select_posts(_sjs_blobs(page, code), code)
    items = [it for p in posts for it in media_items(p)] or _html_media(page)
    if not items:
        raise RuntimeError("media tidak ditemukan di halaman embed")
    return _save(posts, items, dest, code, cb)
