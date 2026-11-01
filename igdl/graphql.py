"""Logged-out GraphQL fetch: the same request instagram.com makes for visitors
without an account.  Returns every carousel slide (photo and video) at full
resolution.  Needs curl_cffi to impersonate a real browser TLS fingerprint.

doc_id / app id are read from the installed yt-dlp source, so
`pip install -U yt-dlp` refreshes them when Instagram rotates the query.
"""
from __future__ import annotations

import inspect
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

Progress = Callable[[str, Optional[float], str], None]

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
FRIENDLY = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
FALLBACK = {"doc_id": "27130156389949648", "app_id": "936619743392459", "asbd": "359341"}
ROOT = "https://www.instagram.com"
SESSION_TTL = 15 * 60
STAGE = "graphql"

_lock = threading.Lock()
_sess: dict = {}          # {"s": Session, "lsd": str, "ts": float}
_params: dict = {}


class GatedError(RuntimeError):
    """Instagram answered, but will not show this post without login."""


def shortcode_to_pk(code: str) -> int:
    if len(code) > 28:              # private-post shortcodes carry 28 extra chars
        code = code[:-28]
    n = 0
    for ch in code:
        n = n * 64 + ALPHABET.index(ch)
    return n


def params() -> dict:
    if _params:
        return _params
    p = dict(FALLBACK)
    try:
        from yt_dlp.extractor import instagram as yi
        src = inspect.getsource(yi)
        i = src.find(FRIENDLY)
        if i >= 0 and (m := re.search(r"'doc_id':\s*'(\d+)'", src[i:])):
            p["doc_id"] = m.group(1)
        if (m := re.search(r"'X-ASBD-ID':\s*'(\d+)'", src)):
            p["asbd"] = m.group(1)
        p["app_id"] = yi.InstagramBaseIE._APP_IDS.get("web", p["app_id"])
    except Exception:
        pass
    _params.update(p)
    return _params


def _api_headers() -> dict:
    p = params()
    return {"X-IG-App-ID": p["app_id"], "X-ASBD-ID": p["asbd"], "X-IG-WWW-Claim": "0",
            "Origin": ROOT, "Accept": "*/*"}


def _new_session():
    try:
        from curl_cffi import requests as cr
    except ImportError as e:
        raise RuntimeError("curl_cffi belum terpasang (jalankan ./install.sh)") from e
    s = cr.Session(impersonate="chrome")
    page = s.get(ROOT + "/", timeout=20).text
    lsd = None
    if (m := re.search(r'<script\b[^>]*\bid="__eqmc"[^>]*>(\{.*?\})</script>', page, re.S)):
        try:
            lsd = json.loads(m.group(1)).get("l")
        except ValueError:
            pass
    if not lsd and (m := re.search(r'\["LSD",\[\],\{"token":"([^"]+)"', page)):
        lsd = m.group(1)
    if not lsd:
        raise RuntimeError("token LSD tidak ada di beranda (diblokir atau formatnya berubah)")
    return {"s": s, "lsd": lsd, "ts": time.monotonic()}


def _session(fresh: bool = False) -> dict:
    with _lock:
        if fresh or not _sess or time.monotonic() - _sess["ts"] > SESSION_TTL:
            _sess.clear()
            _sess.update(_new_session())
        return dict(_sess)


def _find_key(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), dict):
            return obj[key]
        for v in obj.values():
            if (r := _find_key(v, key)) is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            if (r := _find_key(v, key)) is not None:
                return r
    return None


def _query(code: str, ss: dict) -> dict:
    s, lsd = ss["s"], ss["lsd"]
    pk = str(shortcode_to_pk(code))
    ruling = {}
    try:
        ruling = s.get(f"{ROOT}/api/v1/web/get_ruling_for_content/", timeout=20,
                       params={"content_type": "MEDIA", "target_id": pk},
                       headers=_api_headers()).json() or {}
    except Exception:
        pass
    csrf = s.cookies.get("csrftoken") if ruling.get("status") == "ok" else None
    headers = {**_api_headers(), "X-FB-Friendly-Name": FRIENDLY, "X-FB-LSD": lsd,
               "X-Requested-With": "XMLHttpRequest", "Referer": f"{ROOT}/p/{code}/",
               "Content-Type": "application/x-www-form-urlencoded"}
    if csrf:
        headers["X-CSRFToken"] = csrf
    data = {"lsd": lsd, "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": FRIENDLY, "server_timestamps": "true",
            "variables": json.dumps({"media_id": pk}, separators=(",", ":")),
            "doc_id": params()["doc_id"]}
    resp = s.post(f"{ROOT}/api/graphql", data=data, headers=headers, timeout=25)
    if resp.status_code == 429:
        raise RuntimeError("HTTP 429 rate-limit")
    product = None
    try:
        product = ((resp.json().get("data") or {}).get("xig_polaris_media") or {}) \
            .get("if_not_gated_logged_out")
    except ValueError:
        pass
    if product:
        return product

    reason = " ".join(str(ruling.get(k) or "") for k in ("title", "description")).strip()
    if reason:
        raise GatedError(reason)
    # fallback: the post page itself embeds the same payload for logged-out visitors
    page = s.get(f"{ROOT}/p/{code}/", timeout=20)
    if urlparse(str(page.url)).path.startswith("/accounts/login") or urlparse(str(page.url)).path == "/":
        raise RuntimeError("dialihkan ke halaman login (rate-limit akses anonim)")
    for blob in re.findall(r'<script\b[^>]+\bdata-sjs>(\{.+?\})</script>', page.text):
        try:
            media = _find_key(json.loads(blob), "xig_polaris_media")
        except ValueError:
            continue
        if media and isinstance(media.get("if_not_gated_logged_out"), dict):
            return media["if_not_gated_logged_out"]
    if len(code) > 28:
        raise GatedError("post private (hanya untuk follower)")
    raise GatedError("Instagram mengirim respons kosong (post private, dibatasi umur, atau dihapus)")


def fetch_product(code: str) -> dict:
    try:
        return _query(code, _session())
    except GatedError:
        raise
    except Exception:
        return _query(code, _session(fresh=True))     # stale token/cookies → retry once


def _best(cands: list) -> Optional[dict]:
    cands = [c for c in cands or [] if isinstance(c, dict) and c.get("url")]
    return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0), default=None)


def media_items(product: dict) -> list[tuple[str, str]]:
    """[(url, ext)] for every slide, best resolution."""
    out = []
    for m in product.get("carousel_media") or [product]:
        if (v := _best(m.get("video_versions"))):
            out.append((v["url"], ".mp4"))
        elif (img := _best((m.get("image_versions2") or {}).get("candidates"))):
            ext = Path(urlparse(img["url"]).path).suffix.lower()
            out.append((img["url"], ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic") else ".jpg"))
    return out


def download(code: str, dest: Path, cb: Progress) -> list[Path]:
    cb(STAGE, None, "membaca post (tanpa login)")
    items = media_items(fetch_product(code))
    if not items:
        raise RuntimeError("post tidak berisi media")
    s = _session()["s"]
    n, files = len(items), []
    for i, (url, ext) in enumerate(items, 1):
        dst = dest / f"{i:02d}_{code}{ext}"
        part = dst.with_name(dst.name + ".part")
        r = s.get(url, stream=True, timeout=60, headers={"Referer": ROOT + "/"})
        try:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} saat mengunduh file {i}/{n}")
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype.startswith(("text/", "application/json")):
                raise RuntimeError(f"CDN mengirim {ctype.split(';')[0]}, bukan media (file {i}/{n})")
            total = int(r.headers.get("content-length") or 0)
            got, last = 0, 0.0
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    fh.write(chunk)
                    got += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.3:
                        last = now
                        frac = got / total if total else 0.0
                        cb(STAGE, 100.0 * (i - 1 + frac) / n,
                           f"file {i}/{n} · {got / 1e6:.1f} MB")
        finally:
            r.close()
        part.rename(dst)
        files.append(dst)
    cb(STAGE, 100.0, f"{n} file")
    return files
