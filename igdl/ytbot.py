"""YouTube inside the Telegram bot.

link → info (title, duration, size estimates)
     → fits in one ≤47 MB file?  send it right away
     → otherwise reply with buttons: ≤50 MB video · full quality in parts · MP3
playlist → first N videos, each sent automatically (whole if it fits, else the
           ≤50 MB version; too-big ones are listed so they can be sent alone).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import ExitStack
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from . import bot as B
from . import youtube as yt
from .core import IGError
from .media import MB, video_meta

log = logging.getLogger("igdl")

MAX_SECONDS = int(os.environ.get("YT_MAX_MINUTES", "180")) * 60
MAX_PARTS = 20
PLAYLIST_N = int(os.environ.get("YT_PLAYLIST_N", "5"))
PLAYLIST_MAX = 20


# ------------------------------------------------------------------ helpers
async def _retry(make):
    """Run make() (a coroutine factory) with Telegram flood-control retries."""
    for _ in range(4):
        try:
            return await make()
        except RetryAfter as e:
            await asyncio.sleep(B._secs(e.retry_after) + 1)
    return await make()


def _cap(text: str) -> str:
    return text if len(text) <= B.CAPTION_MAX else text[:B.CAPTION_MAX - 1] + "…"


def _mb(x: float) -> str:
    return f"{x / MB:.0f} MB" if x >= 10 * MB else f"{x / MB:.1f} MB"


def _options(p: yt.Plan) -> list[tuple[str, int, str]]:
    """[(mode, height, button label)]"""
    out = []
    if p.fit:
        out.append(("fit", p.fit.height, f"🎬 Video ≤50 MB · {p.fit.height}p · ~{_mb(p.fit.size)}"))
    full = p.full_within(MAX_PARTS)
    if full:
        out.append(("full", full.height,
                    f"✂️ {full.height}p kualitas penuh · {p.parts(full)} bagian"))
    n = p.audio_parts
    out.append(("audio", 0, "🎵 Audio MP3 · " + (f"{n} bagian" if n > 1 else f"~{_mb(p.audio_size)}")))
    return out


def _header(inf: dict) -> str:
    who = f" · {inf['uploader']}" if inf.get("uploader") else ""
    return f"🎬 {inf['title']}\n⏱ {yt.fmt_dur(inf['duration'])}{who}"


async def _info(vid: str) -> dict:
    return await asyncio.to_thread(yt.info, vid)


def _refuse(inf: dict) -> Optional[str]:
    if inf.get("is_live"):
        return "Ini siaran langsung / belum tayang — belum bisa diunduh."
    if inf["duration"] > MAX_SECONDS:
        return (f"Video {yt.fmt_dur(inf['duration'])} melewati batas bot "
                f"({MAX_SECONDS // 3600} jam). Unduh lewat CLI: igdl {yt.watch_url(inf['id'])}")
    return None


# ------------------------------------------------------------------ send
async def _send_files(ctx, chat_id: int, reply_to: int, inf: dict, mode: str, files_or_ids,
                      st: Optional["B.Status"]) -> list[tuple[str, str]]:
    """Each part is its own message with its own caption. Returns [(kind, file_id)]."""
    url = yt.watch_url(inf["id"])
    n = len(files_or_ids)
    sent: list[tuple[str, str]] = []
    for i, item in enumerate(files_or_ids, 1):
        part = f"\nBagian {i}/{n}" if n > 1 else ""
        icon = "🎵" if mode == "audio" else "🎬"
        caption = _cap(f"{icon} {inf['title']}{part}\n{url}")
        if st is not None and n > 1:
            st.update("mengirim ke Telegram", 100.0 * (i - 1) / n, f"bagian {i}/{n}")
        with ExitStack() as stack:
            if isinstance(item, tuple):                 # cached (kind, file_id)
                media, meta = item[1], {}
            else:
                media = stack.enter_context(open(item, "rb"))
                meta = video_meta(item) if mode != "audio" else \
                    {"duration": video_meta(item).get("duration")}
            title = inf["title"] + (f" ({i}/{n})" if n > 1 else "")

            def make(media=media, meta=meta, caption=caption, title=title):
                if hasattr(media, "seek"):
                    media.seek(0)                       # a retry must re-upload from the start
                if mode == "audio":
                    return ctx.bot.send_audio(
                        chat_id, media, caption=caption, title=title[:64],
                        performer=(inf.get("uploader") or "")[:64] or None,
                        duration=meta.get("duration"), reply_to_message_id=reply_to,
                        **B.UPLOAD_TIMEOUTS)
                return ctx.bot.send_video(
                    chat_id, media, caption=caption, supports_streaming=True,
                    reply_to_message_id=reply_to, **meta, **B.UPLOAD_TIMEOUTS)

            m = await _retry(make)
        sent.append(B._file_id(m))
    return sent


async def _deliver(ctx, chat_id: int, reply_to: int, inf: dict, mode: str, height: int,
                   st: "B.Status") -> tuple[int, str]:
    """Download (or reuse) and send one video in one mode. Returns (n_parts, method)."""
    cache: B.Cache = ctx.application.bot_data["cache"]
    key = f"yt:{inf['id']}:{mode}:{height}"
    if cached := cache.get(key):
        try:
            await _send_files(ctx, chat_id, reply_to, inf, mode, [tuple(c) for c in cached], None)
            return len(cached), "cache"
        except BadRequest:
            cache.drop(key)
    files, method = await asyncio.to_thread(
        yt.download, inf["id"], mode, st.update, split_parts=True,
        max_height=height or yt.MAX_HEIGHT)
    total = sum(f.stat().st_size for f in files)
    st.update("mengirim ke Telegram", None, f"{len(files)} file · {_mb(total)}")
    await st.flush()
    sent = await _send_files(ctx, chat_id, reply_to, inf, mode, files, st)
    cache.put(key, sent)
    return len(sent), method


async def _run(ctx, status_msg: Message, chat_id: int, reply_to: int, inf: dict,
               mode: str, height: int):
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    st = B.Status(status_msg, _header(inf))
    if sem.locked():
        await st.flush("⏳ antre…")
    async with sem:
        st.start()
        try:
            n, method = await _deliver(ctx, chat_id, reply_to, inf, mode, height, st)
            st.stop()
            try:
                await status_msg.delete()
            except (BadRequest, NetworkError):
                await st.flush(f"✅ {n} file terkirim")
            log.info("yt ok %s %s/%s: %d file via %s", inf["id"], mode, height, n, method)
        except IGError as e:
            st.stop()
            await st.flush(f"❌ {e}")
        except TimedOut:
            st.stop()
            await st.flush("⚠️ Telegram kehabisan waktu saat upload. File bisa saja tetap muncul; "
                           "kalau tidak, pilih lagi — unduhannya sudah tersimpan di disk.")
        except Exception as e:
            st.stop()
            log.exception("yt gagal %s", inf["id"])
            await st.flush(f"❌ Error: {type(e).__name__}: {str(e)[:300]}")


# ------------------------------------------------------------------ entry points
async def handle_link(ctx: ContextTypes.DEFAULT_TYPE, msg: Message, vid: str):
    status = await msg.reply_text("🎬 membaca info YouTube…", link_preview_options=B.NO_PREVIEW)
    try:
        inf = await _info(vid)
    except IGError as e:
        return await status.edit_text(f"❌ {e}", link_preview_options=B.NO_PREVIEW)
    if reason := _refuse(inf):
        return await status.edit_text(f"{_header(inf)}\n\n❌ {reason}", link_preview_options=B.NO_PREVIEW)
    p = yt.plan(inf)
    if p.full and p.full.size <= yt.PART_LIMIT * 0.95:          # fits whole → just send it
        return await _run(ctx, status, msg.chat_id, msg.message_id, inf, "full", yt.MAX_HEIGHT)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"yt|{mode}|{h}|{vid}")]
                               for mode, h, label in _options(p)])
    full_note = f"Kualitas penuh {p.full.height}p ≈ {_mb(p.full.size)}" if p.full else ""
    await status.edit_text(f"{_header(inf)}\n\n{full_note} — melebihi 50 MB Telegram. Pilih:",
                           reply_markup=kb, link_preview_options=B.NO_PREVIEW)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not B._allowed(update):
        return await q.answer("Akses ditolak", show_alert=True)
    try:
        _, mode, h, vid = q.data.split("|", 3)
        height = int(h)
    except ValueError:
        return await q.answer("Tombol tidak dikenal", show_alert=True)
    await q.answer()
    try:
        inf = await _info(vid)
    except IGError as e:
        return await q.message.edit_text(f"❌ {e}")
    label = next((lb for m, hh, lb in _options(yt.plan(inf)) if m == mode and hh == height), mode)
    try:
        await q.message.edit_text(f"{_header(inf)}\n\n⏳ {label}", link_preview_options=B.NO_PREVIEW)
    except (BadRequest, NetworkError):
        pass
    reply_to = q.message.reply_to_message.message_id if q.message.reply_to_message else q.message.message_id
    await _run(ctx, q.message, q.message.chat_id, reply_to, inf, mode, height)


async def handle_playlist(ctx: ContextTypes.DEFAULT_TYPE, msg: Message, list_id: str, n: int):
    n = max(1, min(n, PLAYLIST_MAX))
    status = await msg.reply_text("📃 membaca playlist…", link_preview_options=B.NO_PREVIEW)
    try:
        title, entries = await asyncio.to_thread(yt.playlist, list_id, n)
    except IGError as e:
        return await status.edit_text(f"❌ {e}")
    sem: asyncio.Semaphore = ctx.application.bot_data["sem"]
    st = B.Status(status, f"📃 {title} · {len(entries)} video")
    ok, skipped = 0, []
    async with sem:
        st.start()
        for i, (vid, vtitle) in enumerate(entries, 1):
            st.update(f"video {i}/{len(entries)}", 100.0 * (i - 1) / len(entries), vtitle[:60])
            try:
                inf = await _info(vid)
                if reason := _refuse(inf):
                    skipped.append(f"{vtitle[:50]}: {reason.split('.')[0]}")
                    continue
                p = yt.plan(inf)
                if p.full and p.full.size <= yt.PART_LIMIT * 0.95:
                    mode, h = "full", yt.MAX_HEIGHT
                elif p.fit:
                    mode, h = "fit", p.fit.height
                else:
                    skipped.append(f"{vtitle[:50]}: terlalu besar — kirim link videonya sendiri")
                    continue
                await _deliver(ctx, msg.chat_id, msg.message_id, inf, mode, h, st)
                ok += 1
                await asyncio.sleep(1.0)
            except IGError as e:
                skipped.append(f"{vtitle[:50]}: {str(e).splitlines()[0]}")
            except TimedOut:
                skipped.append(f"{vtitle[:50]}: Telegram timeout saat upload (mungkin tetap terkirim)")
            except Exception as e:
                log.exception("playlist %s video %s gagal", list_id, vid)
                skipped.append(f"{vtitle[:50]}: {type(e).__name__}")
        st.stop()
    summary = f"✅ {ok}/{len(entries)} video terkirim"
    if skipped:
        summary += "\n" + "\n".join(f"• {s}" for s in skipped[:8])
    await st.flush(summary)
