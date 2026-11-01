"""Per-account downloads.

Listing (which posts exist) and downloading (each post's media) are separate:

* latest N, no login  → web_profile_info (logged-out web API, ~12 newest posts),
                         fallback: gallery-dl's logged-out profile query
* --all, with cookies → gallery-dl lists every post + reel (paginated, throttled)

Every post is then fetched with core.download(), i.e. the same tested chain
(logged-out GraphQL first, so the account is only used for listing) and the
same on-disk cache — a rerun resumes where the previous one stopped.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import graphql
from .core import BASE, IGError, Target, _short, download

ROOT = "https://www.instagram.com"
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
PROFILES = BASE / "data" / "profiles"
ANON_PAGE = 12            # Instagram shows logged-out visitors about one page


@dataclass
class Profile:
    username: str
    user_id: str = ""
    full_name: str = ""
    is_private: bool = False
    post_count: Optional[int] = None
    latest: list[str] = field(default_factory=list)     # newest shortcodes (anon)


def cookies_file() -> Optional[str]:
    """IG_COOKIES from the environment, else from $IGDL_HOME/.env."""
    c = os.environ.get("IG_COOKIES", "").strip()
    if not c and (BASE / ".env").is_file():
        for line in (BASE / ".env").read_text().splitlines():
            if line.strip().startswith("IG_COOKIES="):
                c = line.split("=", 1)[1].strip().strip('"\'')
    if c and Path(c).expanduser().is_file():
        return str(Path(c).expanduser())
    return None


# ------------------------------------------------------------------ profile info
def profile_info(username: str) -> Profile:
    """Logged-out profile lookup. Raises IGError for missing/private accounts."""
    ss = graphql._session()
    r = ss["s"].get(f"{ROOT}/api/v1/users/web_profile_info/", timeout=20,
                    params={"username": username},
                    headers={**graphql._api_headers(), "Referer": f"{ROOT}/{username}/"})
    if r.status_code == 404:
        raise IGError(f"Akun @{username} tidak ditemukan.")
    if r.status_code != 200:
        raise RuntimeError(f"web_profile_info HTTP {r.status_code}")
    try:
        user = (r.json().get("data") or {}).get("user")
    except ValueError:
        raise RuntimeError("web_profile_info: respons bukan JSON (dialihkan ke halaman login?)")
    if not user:
        raise IGError(f"Akun @{username} tidak ditemukan.")
    tl = user.get("edge_owner_to_timeline_media") or {}
    codes = [e["node"]["shortcode"] for e in tl.get("edges") or []
             if (e.get("node") or {}).get("shortcode")]
    return Profile(username=user.get("username") or username, user_id=str(user.get("id") or ""),
                   full_name=user.get("full_name") or "", is_private=bool(user.get("is_private")),
                   post_count=tl.get("count"), latest=codes)


# ------------------------------------------------------------------ listing via gallery-dl
def _gallery_dl_list(url: str, limit: Optional[int], cookies: Optional[str],
                     slow: bool) -> Iterator[str]:
    cmd = [sys.executable, "-m", "gallery_dl", "--print", "post:{post_shortcode}",
           "--http-timeout", "20", "-R", "3", "--sleep-429", "60"]
    if limit:
        cmd += ["--post-range", f"1-{limit}"]
    if cookies:
        cmd += ["-C", cookies]
    if slow:
        cmd += ["--sleep-request", "3-8"]
    cmd.append(url)
    with tempfile.TemporaryFile("w+") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
        n = 0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                code = line.strip()
                if CODE_RE.match(code):
                    n += 1
                    yield code
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        errf.seek(0)
        err = errf.read().strip()
    if n == 0 and proc.returncode not in (0, None):
        raise RuntimeError(_short(err) if err else f"gallery-dl exit {proc.returncode}")


def iter_posts(username: str, limit: Optional[int], everything: bool,
               log: Callable[[str], None]) -> Iterator[str]:
    """Yield unique shortcodes, newest first (posts tab, then reels tab)."""
    seen: set[str] = set()

    def emit(codes):
        for c in codes:
            if c not in seen:
                seen.add(c)
                yield c

    if everything:
        ck = cookies_file()
        if not ck:
            raise IGError("Unduh semua post butuh cookies akun cadangan: isi IG_COOKIES di "
                          f"{BASE / '.env'} (file cookies.txt format Netscape).")
        for tab in ("posts", "reels"):
            log(f"mendaftar tab {tab} (pakai cookies, dengan jeda)…")
            yield from emit(_gallery_dl_list(f"{ROOT}/{username}/{tab}/", None, ck, slow=True))
        return

    want = limit or ANON_PAGE
    errors = []
    try:
        p = profile_info(username)
        if p.is_private:
            raise IGError(f"Akun @{username} private — tidak bisa tanpa login.")
        if p.latest:
            yield from emit(p.latest[:want])
            if len(seen) >= want or not p.post_count or len(seen) >= p.post_count:
                return
    except IGError:
        raise
    except Exception as e:
        errors.append(f"web_profile_info: {_short(str(e))}")
    try:
        before = len(seen)
        yield from emit(_gallery_dl_list(f"{ROOT}/{username}/posts/", want, cookies_file(),
                                         slow=False))
        if len(seen) == before and not seen:
            errors.append("gallery-dl: tidak ada post")
    except Exception as e:
        errors.append(f"gallery-dl: {_short(str(e))}")
    if not seen:
        raise IGError(f"Tidak bisa membaca daftar post @{username} tanpa login.\n\n"
                      + "\n".join(f"• {x}" for x in errors))


# ------------------------------------------------------------------ run
@dataclass
class Result:
    code: str
    files: list[Path]
    method: str
    error: Optional[str] = None


def run(username: str, limit: Optional[int], everything: bool,
        on_post: Callable[[int, Optional[int], Result], None],
        log: Callable[[str], None] = lambda m: None,
        stop: Optional[threading.Event] = None) -> list[Result]:
    """Download posts of an account one by one; on_post(i, total, result) after each."""
    total: Optional[int] = None
    if everything:
        try:
            p = profile_info(username)
            total = p.post_count
            if p.is_private:
                log("akun private: hanya bisa kalau akun cookies mem-follow-nya")
        except IGError:
            raise
        except Exception:
            pass
    else:
        total = limit or ANON_PAGE

    PROFILES.mkdir(parents=True, exist_ok=True)
    index = PROFILES / f"{username.lower()}.jsonl"
    known = set()
    if index.exists():
        known = {json.loads(ln)["code"] for ln in index.read_text().splitlines() if ln.strip()}

    results: list[Result] = []
    fails_in_row = 0
    gap = (3.0, 6.0) if everything else (1.0, 2.5)
    for i, code in enumerate(iter_posts(username, limit, everything, log), 1):
        if stop is not None and stop.is_set():
            break
        if code not in known:
            with open(index, "a") as fh:
                fh.write(json.dumps({"code": code, "seen": int(time.time())}) + "\n")
            known.add(code)
        t = Target(f"{ROOT}/p/{code}/", "post", code)
        res = None
        for attempt in range(2):
            try:
                files, method = download(t)
                res = Result(code, files, method)
                break
            except IGError as e:
                msg = str(e)
                if attempt == 0 and "membatasi" in msg:
                    log("kena rate-limit — jeda 90 detik lalu coba lagi")
                    time.sleep(90)
                    continue
                res = Result(code, [], "", msg.splitlines()[0])
                break
            except Exception as e:
                res = Result(code, [], "", f"{type(e).__name__}: {e}")
                break
        results.append(res)
        on_post(i, total, res)
        fails_in_row = 0 if not res.error else fails_in_row + 1
        if fails_in_row >= 5:
            log("5 post berturut-turut gagal — berhenti. Jalankan lagi nanti; "
                "post yang sudah selesai tidak diunduh ulang.")
            break
        if res.method not in ("cache", ""):
            time.sleep(random.uniform(*gap))          # be gentle with fresh requests
    return results
