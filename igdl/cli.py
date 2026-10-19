"""igdl — download Instagram posts/reels/carousels from the command line."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .core import IGError, classify, download, find_urls, is_video
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
    ap = argparse.ArgumentParser(prog="igdl", description=__doc__)
    ap.add_argument("urls", nargs="*", help="link Instagram (post / reel / carousel)")
    ap.add_argument("-i", "--input", help="file berisi daftar link (satu per baris)")
    ap.add_argument("-o", "--out", default=str(Path.home() / "Downloads" / "igdl"),
                    help="folder hasil (default: ~/Downloads/igdl)")
    ap.add_argument("--fit", type=float, metavar="MB",
                    help="kompres video yang lebih besar dari MB (mis. --fit 49)")
    ap.add_argument("-V", "--version", action="version", version=f"igdl {__version__}")
    a = ap.parse_args(argv)

    urls = []
    for u in a.urls:
        urls += find_urls(u) or [u]
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
        tmp = Path(tempfile.mkdtemp(prefix="igdl_"))
        try:
            files, method = download(t, tmp, bar)
            saved = []
            for f in files:
                if a.fit and is_video(f):
                    f = fit_video(f, int(a.fit * MB), bar)
                name = f"{t.code}_{f.name.replace('_tg.', '.')}" if t.code else f.name
                dst = out / name
                shutil.move(str(f), dst)
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
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if len(urls) > 1:
        sys.stderr.write(f"Selesai: {ok} berhasil, {fail} gagal, "
                         f"{time.monotonic() - t_all:.0f}s → {out}\n")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
