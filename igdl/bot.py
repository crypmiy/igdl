"""igdl-bot — Telegram bot: send an Instagram link, get the photos/videos back."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from telegram import (InputMediaDocument, InputMediaPhoto, InputMediaVideo,
                      LinkPreviewOptions, Message, Update)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (Application, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import __version__
from .core import IGError, Target, classify, download, find_urls, is_video, precheck
from .media import MB, TG_PHOTO_LIMIT, TG_VIDEO_LIMIT, fit_video, normalise_photo, video_meta

log = logging.getLogger("igdl")

BASE = Path(os.environ.get("IGDL_HOME", Path(__file__).resolve().parents[1]))
DATA = BASE / "data"
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1"))
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
EDIT_EVERY = 2.0          # seconds between progress edits (Telegram flood limits)

HELP = ("Kirim link Instagram, nanti saya balas dengan foto/videonya.\n\n"
        "Didukung (tanpa login): post foto, carousel, reel, video.\n"
        "Tidak didukung: story, highlight, profil (butuh login).\n"
        "Video > 50 MB dikompres otomatis.")


# ------------------------------------------------------------------ cache
class Cache:
    """shortcode -> Telegram file_ids, so a repeated link is resent instantly."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, "
                        "items TEXT NOT NULL, ts INTEGER NOT NULL)")
        self.db.commit()

    def get(self, key: str):
        r = self.db.execute("SELECT items FROM sent WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, key: str, items) -> None:
        self.db.execute("INSERT OR REPLACE INTO sent VALUES (?,?,?)",
                        (key, json.dumps(items), int(time.time())))
        self.db.commit()

    def drop(self, key: str) -> None:
        self.db.execute("DELETE FROM sent WHERE key=?", (key,))
        self.db.commit()


# ------------------------------------------------------------------ progress
class Status:
    """Thread-safe progress text, flushed to one Telegram message every few seconds."""

    def __init__(self, msg: Message, title: str):
        self.msg, self.title = msg, title
        self.text = "⏳ menunggu…"
        self.shown = ""
        self.t0 = time.monotonic()
        self._task: asyncio.Task | None = None

    def update(self, stage, pct, detail):          # called from worker threads
        el = time.monotonic() - self.t0
        if pct is not None:
            n = int(pct / 10)
            bar = "▓" * n + "░" * (10 - n)
            eta = f" · ETA {el * (100 - pct) / pct:.0f}s" if 0 < pct < 100 else ""
            self.text = f"⏳ {stage}\n{bar} {pct:.0f}%{eta}\n{detail}"
        else:
            self.text = f"⏳ {stage} · {el:.0f}s\n{detail}"

    async def _loop(self):
        while True:
            await asyncio.sleep(EDIT_EVERY)
            await self.flush()

    async def flush(self, final: str | None = None):
        txt = f"{self.title}\n{final or self.text}"
        if txt == self.shown:
            return
        try:
            await self.msg.edit_text(txt, link_preview_options=NO_PREVIEW)
            self.shown = txt
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except BadRequest:
            pass

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()


# ------------------------------------------------------------------ sending
def _file_id(m: Message) -> tuple[str, str]:
    if m.video:
        return "video", m.video.file_id
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.animation:
        return "video", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    raise ValueError("pesan tanpa media")


def _input_media(kind: str, media, caption=None, **meta):
    if kind == "photo":
        return InputMediaPhoto(media, caption=caption)
    if kind == "video":
        return InputMediaVideo(media, caption=caption, supports_streaming=True, **meta)
    return InputMediaDocument(media, caption=caption)


async def _send(bot, chat_id: int, reply_to: int, items, caption: str):
    """items: list of (kind, media, meta). Albums of ≤10; documents sent apart
    (Telegram cannot mix documents with photos/videos in one album)."""
    sent: list[tuple[str, str]] = []
    groupable = [it for it in items if it[0] in ("photo", "video")]
    docs = [it for it in items if it[0] == "document"]
    first = True

    for i in range(0, len(groupable), 10):
        chunk = groupable[i:i + 10]
        cap = caption if first else None
        if len(chunk) == 1:
            kind, media, meta = chunk[0]
            if kind == "photo":
                m = await bot.send_photo(chat_id, media, caption=cap,
                                         reply_to_message_id=reply_to)
            else:
                m = await bot.send_video(chat_id, media, caption=cap,
                                         supports_streaming=True,
                                         reply_to_message_id=reply_to, **meta)
            sent.append(_file_id(m))
        else:
            group = [_input_media(k, md, cap if j == 0 else None, **mt)
                     for j, (k, md, mt) in enumerate(chunk)]
            msgs = await bot.send_media_group(chat_id, group, reply_to_message_id=reply_to)
            sent += [_file_id(m) for m in msgs]
        first = False
    for kind, media, _ in docs:
        m = await bot.send_document(chat_id, media, caption=caption if first else None,
                                    reply_to_message_id=reply_to)
        sent.append(_file_id(m))
        first = False
    return sent


def _prepare(files: list[Path], st: Status) -> list[tuple[str, Path, dict]]:
    """Runs in a worker thread: compress videos, normalise photos."""
    out = []
    vids = [f for f in files if is_video(f)]
    for n, f in enumerate(files, 1):
        if is_video(f):
            tag = f"video {vids.index(f) + 1}/{len(vids)}" if len(vids) > 1 else "video"
            g = fit_video(f, TG_VIDEO_LIMIT,
                          lambda s, p, d, tag=tag: st.update(f"kompres {tag}", p, d))
            out.append(("video", g, video_meta(g)))
        else:
            g = normalise_photo(f)
            out.append(("photo" if g.stat().st_size <= TG_PHOTO_LIMIT else "document",
                        g, {}))
    return out


# ------------------------------------------------------------------ handlers
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.effective_message.reply_text(
        f"{HELP}\n\nChat ID kamu: {update.effective_chat.id}")


def _allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


async def _deny(update: Update):
    await update.effective_message.reply_text(
        f"Akses ditolak. Chat ID kamu: {update.effective_chat.id}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    msg = update.effective_message
    urls = find_urls(msg.text or msg.caption or "")
    if not urls:
        return await msg.reply_text(HELP)
    for url in urls:
        await process(ctx, msg, url)


async def process(ctx: ContextTypes.DEFAULT_TYPE, msg: Message, url: str):
    app = ctx.application
    cache: Cache = app.bot_data["cache"]
    sem: asyncio.Semaphore = app.bot_data["sem"]
    t: Target = await asyncio.to_thread(classify, url)
    caption = f"📥 {t.url}"

    if reason := precheck(t):
        return await msg.reply_text(f"❌ {reason}")

    if cached := cache.get(t.key):
        try:
            items = [(k, fid, {}) for k, fid in cached]
            await _send(ctx.bot, msg.chat_id, msg.message_id, items, caption + "  ⚡")
            return
        except BadRequest:
            cache.drop(t.key)          # stale file_id → download again

    status_msg = await msg.reply_text(
        f"🔗 {t.code or url}\n⏳ antre…" if sem.locked() else f"🔗 {t.code or url}\n⏳ mulai…",
        link_preview_options=NO_PREVIEW)
    st = Status(status_msg, f"🔗 {t.code or url}")
    tmp = Path(tempfile.mkdtemp(prefix="igdl_"))
    async with sem:
        st.start()
        try:
            files, method = await asyncio.to_thread(download, t, tmp, st.update)
            ready = await asyncio.to_thread(_prepare, files, st)
            total = sum(p.stat().st_size for _, p, _ in ready) / MB
            st.update("mengirim ke Telegram", None, f"{len(ready)} file, {total:.1f} MB")
            await st.flush()
            await ctx.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
            with ExitStack() as stack:
                items = [(k, stack.enter_context(open(p, "rb")), meta) for k, p, meta in ready]
                sent = await _send(ctx.bot, msg.chat_id, msg.message_id, items, caption)
            cache.put(t.key, sent)
            st.stop()
            el = time.monotonic() - st.t0
            log.info("ok %s: %d file %.1f MB via %s in %.1fs", t.key, len(sent), total, method, el)
            try:
                await status_msg.delete()
            except BadRequest:
                await st.flush(f"✅ {len(sent)} file · {total:.1f} MB · {el:.0f}s")
        except IGError as e:
            st.stop()
            await st.flush(f"❌ {e}")
        except Exception as e:
            st.stop()
            log.exception("gagal %s", url)
            await st.flush(f"❌ Error: {type(e).__name__}: {str(e)[:300]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ main
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN belum diisi di .env")
    if not ALLOWED:
        log.warning("ALLOWED_CHAT_IDS kosong — bot terbuka untuk SEMUA orang")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg tidak ditemukan — kompresi video akan gagal")

    app = (Application.builder().token(token)
           .connect_timeout(30).read_timeout(120).write_timeout(300)
           .media_write_timeout(600).pool_timeout(30)
           .concurrent_updates(True).build())
    app.bot_data["cache"] = Cache(DATA / "cache.sqlite")
    app.bot_data["sem"] = asyncio.Semaphore(MAX_JOBS)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text))
    log.info("igdl-bot %s jalan (whitelist: %s, max jobs: %d)",
             __version__, sorted(ALLOWED) or "TIDAK ADA", MAX_JOBS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
