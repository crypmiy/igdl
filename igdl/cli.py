"""igdl — download Instagram posts/reels/carousels, or a whole account, from the command line.

  igdl LINK [LINK…]          post / reel / carousel
  igdl @akun                 12 post terbaru akun (tanpa login)
  igdl @akun -n 5            5 post terbaru
  igdl @akun --all           semua post + reels (butuh IG_COOKIES akun cadangan)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from . import profile as prof
from .core import IGError, classify, download, find_profiles, find_urls, is_video
from .media import MB, fit_video


class Bar:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.t0 = time.monotonic()
        self.last = 0.0

    def __call__(self, stage, pct, detail):
        now = time.monotonic()
        if now - self.last < 0.25 and pct not in (None, 100.0):
            return
        self.last = now
        el = now - self.t0
        if pct is not None:
            n = int(pct / 5)
            bar = "█" * n + "░" * (20 - n)
            eta = f" ETA {el * (100 - pct) / pct:4.0f}s" if 0 < pct < 100 else ""
            line = f"{self.prefix} {stage:<10} {bar} {pct:5.1f}%{eta}  {detail}"
        else:
            line = f"{self.prefix} {stage:<10} … {detail}  ({el:.0f}s)"
        cols = shutil.get_terminal_size((100, 20)).columns
        sys.stderr.write("\r" + line[: cols - 1].ljust(cols - 1))
        sys.stderr.flush()

    def done(self, msg):
        cols = shutil.get_terminal_size((100, 20)).columns
        sys.stderr.write("\r" + " " * (cols - 1) + "\r" + msg + "\n")
        sys.stderr.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="igdl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="link post/reel, link profil, atau @akun")
    ap.add_argument("-n", "--latest", type=int, metavar="N",
                    help="untuk akun: jumlah post terbaru (default 12, tanpa login)")
    ap.add_argument("--all", action="store_true",
                    help="untuk akun: semua post + reels (butuh IG_COOKIES)")
    ap.add_argument("-i", "--input", help="file berisi daftar link (satu per baris)")
    ap.add_argument("-o", "--out", default=str(Path.home() / "Downloads" / "igdl"),
                    help="folder hasil (default: ~/Downloads/igdl)")
    ap.add_argument("--fit", type=float, metavar="MB",
                    help="kompres video yang lebih besar dari MB (mis. --fit 49)")
    ap.add_argument("-V", "--version", action="version", version=f"igdl {__version__}")
    a = ap.parse_args(argv)

    urls = []
    for u in a.urls:
        urls += find_urls(u) or [f"https://www.instagram.com/{x}/" for x in find_profiles(u)] or [u]
    if a.input:
        urls += find_urls(Path(a.input).read_text())
    if not urls:
        ap.print_help()
        return 2

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    t_all = time.monotonic()
    for i, url in enumerate(urls, 1):
        prefix = f"[{i}/{len(urls)}]"
        bar = Bar(prefix)
        t = classify(url)
        if t.kind == "profile":
            good = _account(t.code, a, out)
            ok, fail = ok + good, fail + (not good)
            continue
        try:
            files, method = download(t, None, bar)
            saved = []
            for f in files:
                if a.fit and is_video(f):
                    f = fit_video(f, int(a.fit * MB), bar)
                name = f.name.replace("_tg.", ".")
                if t.code and t.code not in name:
                    name = f"{t.code}_{name}"
                dst = out / name
                shutil.copy2(f, dst)
                saved.append(dst)
            size = sum(p.stat().st_size for p in saved) / MB
            bar.done(f"{prefix} ✓ {t.code or url}: {len(saved)} file, {size:.1f} MB "
                     f"via {method} ({time.monotonic() - bar.t0:.1f}s)")
            for p in saved:
                print(p)
            ok += 1
        except IGError as e:
            bar.done(f"{prefix} ✗ {url}\n    {e}")
            fail += 1
        except Exception as e:
            bar.done(f"{prefix} ✗ {url}\n    {type(e).__name__}: {e}")
            fail += 1
    if len(urls) > 1:
        sys.stderr.write(f"Selesai: {ok} berhasil, {fail} gagal, "
                         f"{time.monotonic() - t_all:.0f}s → {out}\n")
    return 0 if fail == 0 else 1


def _save(f: Path, code: str, dst_dir: Path, fit: Optional[float], bar) -> Path:
    if fit and is_video(f):
        f = fit_video(f, int(fit * MB), bar)
    name = f.name.replace("_tg.", ".")
    if code and code not in name:
        name = f"{code}_{name}"
    dst = dst_dir / name
    if not (dst.exists() and dst.stat().st_size == f.stat().st_size):
        shutil.copy2(f, dst)
    return dst


def _account(username: str, a, out: Path) -> bool:
    dst_dir = out / username
    dst_dir.mkdir(parents=True, exist_ok=True)
    mode = "semua post + reels (cookies)" if a.all else f"{a.latest or prof.ANON_PAGE} post terbaru"
    sys.stderr.write(f"@{username}: {mode} → {dst_dir}\n")
    t0 = time.monotonic()
    stats = {"ok": 0, "fail": 0, "cache": 0, "files": 0, "mb": 0.0}
    bar = Bar(f"@{username}")

    def log(msg):
        bar.done(f"  · {msg}")

    def on_post(i, total, r):
        el = time.monotonic() - t0
        tot = f"/{total}" if total else ""
        eta = ""
        if total and i < total:
            eta = f" · ETA {_fmt(el / i * (total - i))}"
        if r.error:
            stats["fail"] += 1
            bar.done(f"  [{i}{tot}] ✗ {r.code}: {r.error}{eta}")
            return
        saved = [_save(f, r.code, dst_dir, a.fit, bar) for f in r.files]
        mb = sum(p.stat().st_size for p in saved) / MB
        stats["ok"] += 1
        stats["cache"] += r.method == "cache"
        stats["files"] += len(saved)
        stats["mb"] += mb
        bar.done(f"  [{i}{tot}] ✓ {r.code}: {len(saved)} file, {mb:.1f} MB via {r.method}{eta}")

    try:
        prof.run(username, a.latest, a.all, on_post, log)
    except IGError as e:
        bar.done(f"@{username} ✗ {e}")
        return False
    except KeyboardInterrupt:
        bar.done("dihentikan — jalankan perintah yang sama untuk melanjutkan")
        return False
    sys.stderr.write(f"@{username} selesai: {stats['ok']} post ({stats['cache']} dari cache), "
                     f"{stats['fail']} gagal, {stats['files']} file, {stats['mb']:.0f} MB, "
                     f"{_fmt(time.monotonic() - t0)} → {dst_dir}\n")
    return stats["ok"] > 0


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}j{sec % 3600 // 60:02d}m" if sec >= 3600 else \
        f"{sec // 60}m{sec % 60:02d}d" if sec >= 60 else f"{sec}d"


if __name__ == "__main__":
    sys.exit(main())
