# igdl

Unduh foto dan video Instagram, Threads, dan YouTube dari link — lewat **command line** atau **bot Telegram**. Tanpa login.

Dibuat untuk berjalan di server kecil (dikembangkan di Jetson Orin Nano, aarch64), tapi jalan di Linux mana pun yang punya Python ≥ 3.10 dan ffmpeg.

## Fitur

- **Post foto, carousel, reel, dan video** — semua item carousel diunduh berurutan.
- **Post Threads** (`threads.com` / `threads.net`, termasuk link pendek `/t/KODE` dan link bagikan `/share/…`) — foto, video, dan carousel dari post yang di-link **plus lanjutan utas oleh penulisnya**; teks post ikut sebagai caption di bot dan sebagai `KODE_caption.txt` di CLI.
- **Per akun.** `@username` atau link profil → post terbaru tanpa login (±12, batas Instagram untuk pengunjung tanpa akun). Riwayat lengkap (post + reels) lewat CLI `--all` dengan cookies akun cadangan.
- **Tanpa login untuk semua yang lain.** Story dan highlight ditolak dengan pesan yang jelas karena Instagram mewajibkan login untuk itu.
- **Empat jalur unduh berlapis**: GraphQL tanpa login → `gallery-dl` → `yt-dlp` → halaman embed. Kalau satu gagal, jalur berikutnya dicoba otomatis.
- **YouTube** — video, Shorts, dan playlist lewat `yt-dlp` + Deno (dipasang otomatis oleh `install.sh`).
  CLI menyimpan kualitas terbaik s/d 1080p (mp4) atau `--audio` (mp3). Di bot, video yang melebihi
  50 MB memunculkan tombol: **video ≤50 MB** (resolusi yang muat, tanpa encode ulang), **kualitas
  penuh dibagi beberapa bagian** (maks. 20 bagian), atau **audio MP3**.
- **Link `/share/…`** (Instagram maupun Threads) diurai otomatis ke post aslinya; hasilnya disimpan di `data/share_links.json`.
- **Cache di disk**: hasil unduhan disimpan di `data/media/<shortcode>/` dan dipakai ulang; link yang sama tidak diunduh lagi.
- **Bot Telegram**
  - Paste link, bot membalas dengan medianya; carousel dikirim sebagai album (maks. 10 per album).
  - Video di atas 50 MB (batas Bot API) **dikompres otomatis** dengan ffmpeg; resolusi diturunkan sesuai bitrate yang muat.
  - Pesan progres dengan persen dan ETA, diperbarui selama unduh dan kompres.
  - **Cache file_id**: link yang sama dikirim ulang seketika tanpa mengunduh lagi.
  - **Whitelist** chat ID, antrean unduhan (default 1 job).
  - Pesan gagal menyebut alasan tiap jalur, jadi jelas apakah masalahnya jaringan, rate-limit, atau post yang memang butuh login.
- **CLI** dengan progress bar, banyak link sekaligus, input dari file, kompresi opsional.

## Cara kerja

```
link ──► classify (post / reel / story / profil)
           │
           ├─ story / highlight / profil ──► ditolak (butuh login)
           │
           └─ post / reel ──► cache disk? ──ada──► pakai ulang
                                   │ belum
                                   ▼
                     graphql ─gagal─► gallery-dl ─gagal─► yt-dlp ─gagal─► embed
                        │                 │                 │              │
                        └──────────── file media (data/media/<kode>/) ◄────┘
                                                   │
                         CLI: simpan ke folder     │     Bot: kompres video > 50 MB,
                         (--fit opsional)          │     konversi webp/heic → jpg,
                                                   ▼     kirim album, simpan file_id
```

## Instalasi

Butuh: Linux, Python ≥ 3.10, `sudo` (untuk ffmpeg dan service systemd).

```bash
git clone https://github.com/crypmiy/igdl.git ~/igdl
cd ~/igdl && ./install.sh
```

`install.sh` aman dijalankan berulang (juga dipakai untuk upgrade). Ia akan:

1. memasang `ffmpeg` dan `python3-venv` bila belum ada,
2. membuat `venv/` dan memasang dependensi,
3. membuat `.env` dari `.env.example` (tidak menimpa yang sudah ada),
4. membuat perintah `igdl` di `~/.local/bin`,
5. memasang service systemd `igdl-bot`.

## Pemakaian CLI

```bash
igdl https://www.instagram.com/p/XXXXXXXXXXX/          # simpan ke ~/Downloads/igdl
igdl LINK1 LINK2 -o ~/ig                               # banyak link, folder lain
igdl -i links.txt                                      # daftar link dari file
igdl -i links.txt --fit 49                             # kompres video > 49 MB
igdl https://www.threads.com/@akun/post/XXXXXXXXXXX    # post Threads (+ lanjutan utas penulis)
```

**Threads — apa yang ikut:** post yang di-link, post berikutnya dari penulis yang sama di utas itu,
dan (kalau post-nya adalah post utama) utas balasan yang diawali penulisnya. Tidak ikut: post
induk di atasnya, balasan orang lain, post yang dikutip/di-repost, dan "post lainnya" di halaman.
Post yang hanya berisi teks dibalas dengan pesan yang jelas.

### YouTube

```bash
igdl https://youtu.be/XXXXXXXXXXX                 # mp4 terbaik s/d 1080p
igdl https://youtu.be/XXXXXXXXXXX --res 720       # batasi resolusi
igdl https://youtu.be/XXXXXXXXXXX --audio         # mp3 128 kbps
igdl "https://www.youtube.com/playlist?list=PL…"  # semua video playlist → folder sendiri
igdl "https://www.youtube.com/playlist?list=PL…" -n 5 --audio
```

Nama file: `Judul [ID].mp4`. Video dengan batas umur, khusus member, atau private dibalas dengan
pesan yang jelas (butuh login YouTube, belum didukung). Siaran langsung belum bisa diunduh.

### Per akun

```bash
igdl @natgeo                       # 12 post terbaru, tanpa login → ~/Downloads/igdl/natgeo/
igdl @natgeo -n 5                  # 5 post terbaru
igdl https://www.instagram.com/natgeo/
igdl @natgeo --all                 # semua post + reels (butuh IG_COOKIES, lihat di bawah)
```

- Progres per post: `[37/412] ✓ KODE: 3 file via graphql · ETA 41m`.
- **Tahan putus:** Ctrl+C atau koneksi terputus tidak masalah — jalankan perintah yang sama dan
  post yang sudah selesai diambil dari cache, tidak diunduh ulang.
- Kena rate-limit → jeda 90 detik lalu coba lagi; 5 post gagal berturut-turut → berhenti dengan
  pesan "jalankan lagi nanti".
- Daftar post yang pernah terlihat per akun disimpan di `data/profiles/<akun>.jsonl`.

#### `--all` (riwayat lengkap)

Tanpa login, Instagram hanya memperlihatkan satu halaman (±12 post) per profil. Untuk riwayat
lengkap, igdl memakai cookies **akun cadangan** — jangan akun utama, akun yang dipakai scraping
bisa di-suspend.

1. Login ke akun cadangan di browser, ekspor cookies `instagram.com` ke `cookies.txt`
   (format Netscape, mis. ekstensi "Get cookies.txt LOCALLY").
2. Simpan sebagai `~/igdl/cookies.txt` (`chmod 600`), lalu isi `IG_COOKIES=/home/jetson/igdl/cookies.txt` di `.env`.

Akun itu **hanya dipakai untuk mendaftar** post (tab posts lalu reels, dengan jeda 3–8 detik per
request). Medianya tetap diunduh lewat jalur GraphQL tanpa login. Akun dengan ribuan post butuh
beberapa jam — jedanya disengaja.

CLI menyimpan kualitas asli; kompresi hanya dilakukan bila diberi `--fit`. Path file hasil dicetak ke stdout, progres ke stderr, jadi aman dipakai di skrip.

## Pemakaian bot Telegram

1. Buat bot lewat [@BotFather](https://t.me/BotFather), salin token-nya.
2. Isi `BOT_TOKEN` di `~/igdl/.env`.
3. Jalankan:
   ```bash
   sudo systemctl enable --now igdl-bot
   journalctl -u igdl-bot -f
   ```
4. Kirim `/start` ke bot — ia membalas dengan **Chat ID** kamu.
5. Isi `ALLOWED_CHAT_IDS` di `.env`, lalu `sudo systemctl restart igdl-bot`.

Setelah itu cukup paste satu atau beberapa link Instagram dalam satu pesan.

**YouTube di bot:** paste link video → bot membalas judul + durasi. Kalau muat satu file ≤50 MB
langsung dikirim; kalau tidak, pilih lewat tombol. Tiap bagian dikirim sebagai pesan sendiri
("Bagian 2/6"), audio sebagai file musik. Link playlist → N video pertama (default 5, mis.
`LINK_PLAYLIST 3`), masing-masing otomatis (utuh kalau muat, kalau tidak versi ≤50 MB). Batas
durasi di bot 3 jam per video (`YT_MAX_MINUTES`); yang lebih panjang lewat CLI.

**Per akun di bot:** kirim `@natgeo` atau link profil → bot mengirim post terbaru satu per satu
(default 12, ubah lewat angka: `@natgeo 5`, maks. 50). Unduhan post berikutnya berjalan sambil
post sebelumnya dikirim, dan bot otomatis menunggu bila kena batas flood Telegram. Riwayat
lengkap sengaja hanya lewat CLI.

## Konfigurasi (`.env`)

| Variabel | Default | Keterangan |
|---|---|---|
| `BOT_TOKEN` | — | Token dari @BotFather (wajib untuk bot). |
| `ALLOWED_CHAT_IDS` | kosong | Chat ID yang boleh memakai bot, dipisah koma. **Kosong = terbuka untuk semua orang.** |
| `MAX_JOBS` | `1` | Jumlah unduhan paralel. Kompresi memakai CPU, jadi 1 sudah cukup di perangkat kecil. |
| `YT_MAX_MINUTES` | `180` | Batas durasi video YouTube di bot. |
| `YT_PLAYLIST_N` | `5` | Jumlah video pertama yang dikirim bot untuk link playlist (maks. 20). |
| `YT_COOKIES` | kosong | Opsional: `cookies.txt` YouTube (untuk video dengan batas umur / verifikasi "bukan bot"). |
| `PROFILE_N` | `12` | Jumlah post terbaru yang dikirim bot untuk `@akun`. |
| `IG_COOKIES` | kosong | Opsional: path `cookies.txt` akun cadangan (format Netscape). Dipakai untuk `igdl @akun --all` dan sebagai cadangan gallery-dl. Kosong = tanpa login. |

## Cara kerja jalur GraphQL

Jalur pertama meniru permintaan yang dibuat instagram.com untuk pengunjung **tanpa akun**
(`PolarisLoggedOutDesktopWWWPostRootContentQuery`), memakai `curl_cffi` agar sidik jari TLS-nya
sama dengan browser. Hasilnya semua slide carousel, foto maupun video, dalam resolusi penuh.
`doc_id` query dibaca dari source yt-dlp yang terpasang, jadi `pip install -U yt-dlp` sekaligus
memperbarui igdl kalau Instagram mengganti query-nya.

`gallery-dl` versi sekarang memakai REST API yang tanpa login hampir selalu dialihkan ke halaman
login; ia tetap ada di rantai untuk dipakai bersama cookies. `yt-dlp` hanya mengambil video.

## Perawatan

Instagram dan YouTube sering mengubah sistemnya. Kalau unduhan mulai gagal, jalankan ulang installer:

```bash
cd ~/igdl && ./install.sh          # selalu memperbarui yt-dlp + gallery-dl ke versi terbaru
```

Cache media bisa dihapus kapan saja bila disk penuh:

```bash
du -sh ~/igdl/data/media && rm -rf ~/igdl/data/media/*
```

Upgrade igdl sendiri:

```bash
cd ~/igdl && git pull && ./install.sh
```

## Batasan

- Hanya konten **publik**. Akun private, story, dan highlight tidak didukung tanpa login.
- Tanpa login, per akun hanya ±12 post terbaru. Reels yang tidak tampil di grid profil hanya ikut dengan `--all`.
- Instagram membatasi akses tanpa login (rate-limit). Bila sering gagal, tunggu beberapa menit.
- Kompresi memakai encoder software (libx264). Di perangkat tanpa encoder hardware, video panjang butuh waktu; progresnya tetap terlihat.
- Batas Bot API Telegram: 50 MB per file, 10 item per album.

## Struktur

```
igdl/
├── igdl/
│   ├── core.py      # klasifikasi link, cache, rantai unduh
│   ├── graphql.py   # jalur GraphQL tanpa login (curl_cffi)
│   ├── profile.py   # unduhan per akun (daftar post + unduh satu per satu)
│   ├── threads.py   # post Threads tanpa login (halaman post → embed)
│   ├── youtube.py   # YouTube: info, pilih format, unduh, potong, mp3, playlist
│   ├── ytbot.py     # YouTube di bot: tombol pilihan, kirim per bagian
│   ├── media.py     # ffprobe/ffmpeg: kompres video, normalisasi foto
│   ├── cli.py       # perintah `igdl`
│   └── bot.py       # perintah `igdl-bot` (Telegram)
├── install.sh
├── igdl-bot.service.in
├── .env.example
└── pyproject.toml
```

## Catatan penggunaan

Gunakan untuk keperluan pribadi. Hormati hak cipta pemilik konten dan Ketentuan Layanan Instagram; jangan unggah ulang karya orang lain tanpa izin.
