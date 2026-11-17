#!/usr/bin/env bash
# igdl installer — idempotent, also used for upgrades.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${USER:-$(id -un)}"
cd "$DIR"
step() { printf '\n\033[1;36m[%s/7] %s\033[0m\n' "$1" "$2"; }

step 1 "cek ffmpeg & python3-venv"
missing=()
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
python3 -c "import ensurepip" 2>/dev/null || missing+=(python3-venv)
if ((${#missing[@]})); then
  echo "  memasang: ${missing[*]}"
  sudo apt-get update -qq && sudo apt-get install -y -qq "${missing[@]}"
fi
echo "  ffmpeg: $(ffmpeg -version | head -1 | cut -d' ' -f3)"

step 2 "venv"
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip

step 3 "install igdl + gallery-dl + yt-dlp + python-telegram-bot"
venv/bin/pip install -q --upgrade --progress-bar on -e .
# extractors break whenever Instagram/YouTube change something → always take the newest
venv/bin/pip install -q --upgrade "yt-dlp[default,curl-cffi]" gallery-dl
venv/bin/python -c "import gallery_dl.version as g, yt_dlp.version as y, telegram as t; \
print(f'  gallery-dl {g.__version__} | yt-dlp {y.__version__} | ptb {t.__version__}')"

step 4 "Deno (JavaScript runtime yang dipakai yt-dlp untuk YouTube)"
mkdir -p bin
if [ -x bin/deno ] && bin/deno --version >/dev/null 2>&1; then
  echo "  sudah ada: $(bin/deno --version | head -1)"
elif command -v deno >/dev/null 2>&1; then
  ln -sf "$(command -v deno)" bin/deno; echo "  pakai deno sistem: $(bin/deno --version | head -1)"
else
  case "$(uname -m)" in
    aarch64|arm64) T=aarch64-unknown-linux-gnu ;;
    x86_64|amd64)  T=x86_64-unknown-linux-gnu ;;
    *) echo "  arsitektur $(uname -m) tidak didukung Deno — YouTube mungkin gagal"; T= ;;
  esac
  if [ -n "$T" ]; then
    venv/bin/python - "$T" <<'PY'
import io, sys, time, urllib.request, zipfile, os
url = f"https://github.com/denoland/deno/releases/latest/download/deno-{sys.argv[1]}.zip"
with urllib.request.urlopen(url, timeout=60) as r:
    total, buf, t0 = int(r.headers.get("content-length") or 0), io.BytesIO(), time.time()
    while chunk := r.read(1 << 20):
        buf.write(chunk)
        pct = f"{100 * buf.tell() / total:5.1f}%" if total else ""
        print(f"\r  unduh deno {buf.tell() / 1e6:5.1f} MB {pct}", end="", flush=True)
print(f"  ({time.time() - t0:.0f}s)")
zipfile.ZipFile(buf).extract("deno", "bin")
os.chmod("bin/deno", 0o755)
PY
    echo "  $(bin/deno --version | head -1)"
  fi
fi

step 5 ".env & folder data"
mkdir -p data
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; echo "  .env dibuat — isi BOT_TOKEN"; else echo "  .env sudah ada (tidak diubah)"; fi

step 6 "perintah igdl di PATH"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/venv/bin/igdl" "$HOME/.local/bin/igdl"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo '  catatan: tambahkan ~/.local/bin ke PATH (atau buka terminal baru)';; esac

step 7 "systemd service igdl-bot"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@DIR@|$DIR|g" igdl-bot.service.in \
  | sudo tee /etc/systemd/system/igdl-bot.service >/dev/null
sudo systemctl daemon-reload
if systemctl is-active --quiet igdl-bot; then sudo systemctl restart igdl-bot; echo "  service di-restart"; fi

printf '\n\033[1;32mSelesai.\033[0m\n'
grep -q '^BOT_TOKEN=.\+' .env || echo "  Langkah berikut: nano $DIR/.env → isi BOT_TOKEN, lalu: sudo systemctl enable --now igdl-bot"
