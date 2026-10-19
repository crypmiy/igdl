# igdl — Instagram downloader (CLI + Telegram bot)

Mengunduh **post foto, carousel, reel, dan video** Instagram tanpa login.
Story / highlight / profil sengaja ditolak (butuh login).

Rantai unduh: **gallery-dl → yt-dlp → halaman embed** (berhenti di yang pertama berhasil).

## Pasang (Jetson)

```bash
cd ~ && tar xzf igdl.tar.gz && cd igdl && ./install.sh
nano ~/igdl/.env                         # isi BOT_TOKEN
sudo systemctl enable --now igdl-bot
journalctl -u igdl-bot -f                # lihat log
```

Kirim `/start` ke bot → bot membalas Chat ID kamu → isi `ALLOWED_CHAT_IDS` di `.env`
→ `sudo systemctl restart igdl-bot`.

## CLI

```bash
igdl https://www.instagram.com/p/XXXX/                 # simpan ke ~/Downloads/igdl
igdl LINK1 LINK2 -o ~/ig                               # banyak link, folder lain
igdl -i links.txt --fit 49                             # dari file, kompres video > 49 MB
```

CLI menyimpan kualitas asli; kompresi hanya kalau diberi `--fit`.

## Bot

- Paste satu atau beberapa link dalam satu pesan.
- Carousel dikirim sebagai album (maks 10 per album).
- Video > 50 MB dikompres otomatis (libx264, resolusi diturunkan sesuai bitrate).
- Link yang sudah pernah dikirim dibalas instan dari cache (file_id Telegram).

## Saat Instagram berubah dan unduhan mulai gagal

```bash
~/igdl/venv/bin/pip install -U gallery-dl yt-dlp && sudo systemctl restart igdl-bot
```

## Login (nanti, opsional)

Isi `IG_COOKIES=/home/jetson/igdl/cookies.txt` (format Netscape, dari akun cadangan).
Kode sudah meneruskannya ke gallery-dl dan yt-dlp; story tetap ditolak sampai fiturnya dibuka.
