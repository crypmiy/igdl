#!/usr/bin/env bash
# igdl installer — idempotent, also used for upgrades.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${USER:-$(id -un)}"
cd "$DIR"
step() { printf '\n\033[1;36m[%s/6] %s\033[0m\n' "$1" "$2"; }

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
venv/bin/python -c "import gallery_dl.version as g, yt_dlp.version as y, telegram as t; \
print(f'  gallery-dl {g.__version__} | yt-dlp {y.__version__} | ptb {t.__version__}')"

step 4 ".env & folder data"
mkdir -p data
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; echo "  .env dibuat — isi BOT_TOKEN"; else echo "  .env sudah ada (tidak diubah)"; fi

step 5 "perintah igdl di PATH"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/venv/bin/igdl" "$HOME/.local/bin/igdl"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo '  catatan: tambahkan ~/.local/bin ke PATH (atau buka terminal baru)';; esac

step 6 "systemd service igdl-bot"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@DIR@|$DIR|g" igdl-bot.service.in \
  | sudo tee /etc/systemd/system/igdl-bot.service >/dev/null
sudo systemctl daemon-reload
if systemctl is-active --quiet igdl-bot; then sudo systemctl restart igdl-bot; echo "  service di-restart"; fi

printf '\n\033[1;32mSelesai.\033[0m\n'
grep -q '^BOT_TOKEN=.\+' .env || echo "  Langkah berikut: nano $DIR/.env → isi BOT_TOKEN, lalu: sudo systemctl enable --now igdl-bot"
