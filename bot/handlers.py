"""Telegram message handlers."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from bot import messages as msg
from bot.admin import admin_keyboard_for_start, admin_settings_input
from bot.config import is_admin
from bot import inline_cache
from bot import media_cache
from bot import quality_pending
from bot.downloader import (
    FileTooLargeError,
    MediaResult,
    available_video_heights,
    cleanup_file,
    download_from_info,
    extract_media_info,
    extract_urls,
    resolve_media,
    thumbnail_url_from_info,
)
from bot import stats
from bot.jobs import DownloadCancelledError, register_job, request_cancel, unregister_job
from bot.messages import detect_platform
from bot.quality import needs_quality_picker
from bot.uploader import send_media
from bot.urls import extract_any_urls_from_message, extract_urls_from_message

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply_markup = None
    chat = update.effective_chat
    if (
        update.effective_user
        and is_admin(update.effective_user.id)
        and chat
        and chat.type == ChatType.PRIVATE
    ):
        reply_markup = admin_keyboard_for_start()
    await update.message.reply_text(
        msg.start_text(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        msg.HELP_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        msg.about_text(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel admin credential input, a quality picker, or an active download/upload."""
    from bot.admin import AWAIT_API_HASH, AWAIT_API_ID, _require_admin_dm, cancel_admin_input

    if (
        update.effective_user
        and _require_admin_dm(update)
        and (context.user_data.get(AWAIT_API_ID) or context.user_data.get(AWAIT_API_HASH))
    ):
        await cancel_admin_input(update, context)
        return

    user = update.effective_user
    if not user:
        return

    cleared = quality_pending.clear_user(user.id)
    if request_cancel(user.id):
        await update.message.reply_text("🛑 Cancelling current download/upload…")
        return

    if cleared:
        await update.message.reply_text("🛑 Quality selection cancelled.")
        return

    await update.message.reply_text("Nothing in progress to cancel.")


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline mode: prepare in background; answer Send when ready (before query expires)."""
    inline = update.inline_query
    raw_query = (inline.query or "").strip()
    query = inline_cache.strip_inline_query(raw_query)
    user_id = inline.from_user.id if inline.from_user else 0
    t0 = time.monotonic()

    logger.info(
        "Inline query from user %s: raw=%r cleaned=%r",
        user_id or "?",
        raw_query[:120],
        query[:120],
    )

    if not query:
        await inline.answer(
            [
                InlineQueryResultArticle(
                    id="help",
                    title="Paste a video or music link",
                    description="Type: @bot https://youtube.com/watch?v=…",
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            "Paste a full URL after @bot.\n"
                            "When the file is ready, tap <b>Send</b> to post it here."
                        ),
                        parse_mode=ParseMode.HTML,
                    ),
                )
            ],
            cache_time=15,
            is_personal=True,
        )
        return

    urls = extract_urls(query)
    if not urls:
        logger.info("Inline query had no supported URLs after parse")
        await inline.answer([], cache_time=5, is_personal=True)
        return

    # Fast path: all links already have a Telegram file_id — answer immediately
    results: list = []
    need_prepare: list[str] = []
    for url in urls[:5]:
        cached = media_cache.get_cached(url)
        if cached and cached.file_id:
            name, emoji = msg.detect_platform(url)
            results.append(_inline_cached_result(url, cached, name, emoji))
            logger.info(
                "Inline cache hit %s → file_id (instant Send)",
                media_cache.cache_key(url),
            )
            inline_cache.clear_prepare(url)
        else:
            need_prepare.append(url)

    if not need_prepare:
        try:
            await inline.answer(results, cache_time=60, is_personal=True)
        except (BadRequest, TelegramError) as exc:
            logger.warning("Inline answer failed (cached): %s", exc)
        return

    # Cold path: start / reuse prepares. Telegram expires query_ids in ~10s —
    # wait a bit for fast links, then answer (empty if still preparing).
    for url in need_prepare:
        started = inline_cache.begin_prepare(url, user_id, force_retry=True)
        if started is not None:
            task = asyncio.create_task(
                _inline_background_prepare(context.bot, user_id, url)
            )
            inline_cache.register_task(url, task)
            logger.info(
                "Inline cache miss %s — prepare queued",
                media_cache.cache_key(url),
            )
        else:
            st = inline_cache.get_prepare(url)
            logger.info(
                "Inline prepare reuse %s status=%s task=%s",
                media_cache.cache_key(url),
                st.status if st else None,
                bool(inline_cache.get_task(url)),
            )

    # Stay under Telegram's inline-query deadline (spinner dies if we answer too late)
    await asyncio.gather(
        *(inline_cache.wait_until_ready(u, timeout=5.0) for u in need_prepare)
    )

    for url in need_prepare:
        name, emoji = msg.detect_platform(url)
        cached = media_cache.get_cached(url)
        if cached and cached.file_id:
            results.append(_inline_cached_result(url, cached, name, emoji))
            inline_cache.clear_prepare(url)
        else:
            st = inline_cache.get_prepare(url)
            logger.info(
                "Inline not ready yet %s status=%s err=%s (%.1fs)",
                media_cache.cache_key(url),
                st.status if st else None,
                (st.error[:80] if st and st.error else None),
                time.monotonic() - t0,
            )

    try:
        await inline.answer(results, cache_time=5 if not results else 30, is_personal=True)
        logger.info(
            "Inline answered user=%s results=%d elapsed=%.1fs",
            user_id,
            len(results),
            time.monotonic() - t0,
        )
    except (BadRequest, TelegramError) as exc:
        # Common on slow VPS: "query is too old" — prepare keeps running for next try
        logger.warning(
            "Inline answer failed after %.1fs (%d results): %s",
            time.monotonic() - t0,
            len(results),
            exc,
        )


def _inline_cached_result(url: str, cached, name: str, emoji: str):
    title = (cached.title or f"{name} media")[:64]
    desc = f"{msg.truncate_url(url, 48)} · tap to send"
    if cached.is_image:
        return InlineQueryResultCachedPhoto(
            id=f"img{abs(hash(url)) % 10**10}",
            photo_file_id=cached.file_id,
            title=f"📤 Send {emoji} {title}",
            description=desc,
            caption=msg.caption(
                cached.title or title,
                url,
                is_image=True,
                file_size=cached.file_size,
            ),
            parse_mode=ParseMode.HTML,
        )
    if cached.is_audio:
        return InlineQueryResultCachedAudio(
            id=f"aud{abs(hash(url)) % 10**10}",
            audio_file_id=cached.file_id,
            caption=msg.caption(
                cached.title or title,
                url,
                is_audio=True,
                file_size=cached.file_size,
            ),
            parse_mode=ParseMode.HTML,
        )
    return InlineQueryResultCachedVideo(
        id=f"vid{abs(hash(url)) % 10**10}",
        video_file_id=cached.file_id,
        title=f"📤 Send {emoji} {title}",
        description=desc,
        caption=msg.caption(
            cached.title or title,
            url,
            file_size=cached.file_size,
        ),
        parse_mode=ParseMode.HTML,
    )


def _inline_single_from_album(result):
    """Use the first carousel item for inline (multi-send isn't supported)."""
    from bot.downloader import MediaResult

    if not result.album:
        return result
    first = result.album[0]
    return MediaResult(
        title=result.title or "media",
        is_audio=False,
        file_size=first.file_size,
        direct_url=first.url,
        file_path=first.path,
        used_direct=bool(first.url and not first.path),
        is_image=(first.kind == "image"),
        uploader=result.uploader,
    )


async def _inline_background_prepare(bot, user_id: int, url: str) -> None:
    """Download + upload to get a Telegram file_id (no user DMs)."""
    from bot.uploader import materialize_file_id

    name, _emoji = msg.detect_platform(url)
    logger.info("Inline prepare start user=%s url=%s", user_id, url[:100])
    loop = asyncio.get_running_loop()
    result = None
    try:
        result = await loop.run_in_executor(
            None,
            lambda: resolve_media(url, progress_callback=None),
        )
    except Exception as exc:
        logger.exception("Inline prepare download failed for %s", url[:80])
        inline_cache.mark_prepare_error(url, msg.friendly_error(str(exc)))
        return

    if not result:
        inline_cache.mark_prepare_error(url, "No media found")
        return

    if result.album and len(result.album) > 1:
        logger.info(
            "Inline prepare using first of %d album items for %s",
            len(result.album),
            url[:80],
        )
        result = _inline_single_from_album(result)

    caption = _media_caption(url, result)
    try:
        file_id, kind = await materialize_file_id(bot, user_id, result, caption=caption)
    except TelegramError as exc:
        logger.warning("Inline materialize failed user=%s: %s", user_id, exc)
        err = (
            "Open a private chat with me and tap /start, then try the link again."
            if "chat not found" in str(exc).lower() or "blocked" in str(exc).lower()
            else msg.friendly_error(str(exc))
        )
        inline_cache.mark_prepare_error(url, err)
        if result.needs_cleanup and result.file_path:
            cleanup_file(result.file_path)
        return
    except Exception as exc:
        logger.exception("Inline materialize error")
        inline_cache.mark_prepare_error(url, msg.friendly_error(str(exc)))
        if result.needs_cleanup and result.file_path:
            cleanup_file(result.file_path)
        return

    if result.needs_cleanup and result.file_path:
        cleanup_file(result.file_path)

    title = result.title or name
    media_cache.store_cached(
        url,
        file_id,
        is_audio=(kind == "audio"),
        is_image=(kind == "photo"),
        title=title,
        file_size=result.file_size,
    )
    inline_cache.mark_prepare_ready(url, title=title)
    logger.info("Inline prepare ready user=%s kind=%s url=%s", user_id, kind, url[:100])


async def chosen_inline_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log only — media is delivered via Cached* results after background prepare."""
    chosen = update.chosen_inline_result
    if not chosen or not chosen.from_user:
        return
    logger.info(
        "Inline result chosen by user %s result_id=%s query=%r",
        chosen.from_user.id,
        chosen.result_id,
        (chosen.query or "")[:100],
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    if await admin_settings_input(update, context):
        return

    urls = extract_urls_from_message(message)
    if not urls:
        # Record unsupported hosts for admin; reply only in private chats
        any_urls = extract_any_urls_from_message(message)
        if any_urls:
            user = update.effective_user
            chat = update.effective_chat
            try:
                platform, _ = detect_platform(any_urls[0])
                stats.record_failure(
                    kind="unsupported",
                    url=any_urls[0],
                    error="Unsupported site",
                    user_id=user.id if user else None,
                    username=user.username if user else None,
                    chat_id=chat.id if chat else None,
                    chat_type=chat.type if chat else None,
                    platform=platform if platform != "Link" else None,
                )
            except Exception:
                logger.exception("Failed to record unsupported link")
            if chat and chat.type == ChatType.PRIVATE:
                await message.reply_text(
                    msg.unsupported_link_message(any_urls[0]),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        return

    chat = update.effective_chat
    via_bot = message.via_bot.username if message.via_bot else None
    logger.info(
        "Link(s) in %s chat %s from user %s: %d URL(s)%s",
        chat.type if chat else "unknown",
        chat.id if chat else "?",
        update.effective_user.id if update.effective_user else "?",
        len(urls),
        f" via @{via_bot}" if via_bot else "",
    )

    if len(urls) > 1:
        await message.reply_text(
            msg.batch_notice(len(urls)),
            parse_mode=ParseMode.HTML,
        )

    total = len(urls)
    batch_cancelled = False
    for index, url in enumerate(urls, start=1):
        if batch_cancelled:
            break
        try:
            await _process_url(update, context, url, index=index, total=total)
        except DownloadCancelledError:
            batch_cancelled = True


def _quality_keyboard(token: str, heights: list[int]) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(f"{h}p", callback_data=f"q:{token}:{h}")
        for h in heights
    ]
    rows = [row] if len(row) <= 8 else [row[:4], row[4:]]
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data=f"q:{token}:cancel")])
    return InlineKeyboardMarkup(rows)


async def _maybe_show_quality_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    message,
    status_msg,
    user,
    index: int,
    total: int,
    cancel_check,
) -> bool:
    """Extract formats; if 2+ ladder heights, show picker and return True."""
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(
            None,
            lambda: extract_media_info(url, cancel_check=cancel_check),
        )
    except DownloadCancelledError:
        raise
    except Exception as exc:
        logger.warning("Quality extract failed for %s: %s — auto-download", url[:80], exc)
        return False

    heights = available_video_heights(info)
    if len(heights) < 2:
        return False

    thumb = thumbnail_url_from_info(info)
    title = info.get("title")
    uploader = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or info.get("uploader_id")
    )
    duration = info.get("duration")
    try:
        duration_i = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_i = None

    pending = quality_pending.create(
        user_id=user.id,
        url=url,
        chat_id=message.chat_id,
        reply_to_message_id=message.message_id,
        index=index,
        total=total,
        heights=heights,
        title=str(title) if title else None,
        uploader=str(uploader) if uploader else None,
        duration=duration_i,
        thumbnail=thumb,
        info=info,
    )
    keyboard = _quality_keyboard(pending.token, heights)
    caption = msg.quality_picker_caption(
        url,
        title=pending.title,
        uploader=pending.uploader,
        duration=pending.duration,
        index=index,
        total=total,
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    picker_msg = None
    if thumb:
        try:
            picker_msg = await message.reply_photo(
                photo=thumb,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except (BadRequest, TelegramError) as exc:
            logger.warning("Quality thumbnail failed for %s: %s", url[:80], exc)

    if picker_msg is None:
        picker_msg = await message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    quality_pending.set_picker_message_id(pending.token, picker_msg.message_id)
    logger.info(
        "Quality picker for %s heights=%s token=%s",
        url[:80],
        heights,
        pending.token,
    )
    return True


async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle q:{token}:{height}|cancel from the quality picker."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "q":
        return
    token, action = parts[1], parts[2]

    pending = quality_pending.get(token)
    if not user or not pending or pending.user_id != user.id:
        quality_pending.pop(token)
        try:
            await query.answer("This selection expired.", show_alert=True)
        except Exception:
            pass
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        return

    if action == "cancel":
        quality_pending.pop(token)
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        return

    try:
        height = int(action)
    except ValueError:
        return

    if height not in pending.heights:
        await query.answer("That quality is no longer available.", show_alert=True)
        return

    quality_pending.pop(token)
    url = pending.url
    index = pending.index
    total = pending.total

    status_text = msg.status_message(
        url,
        f"⬇️ <b>Downloading {height}p…</b>",
        index=index,
        total=total,
    )
    status_msg = query.message
    try:
        if status_msg and status_msg.photo:
            await status_msg.edit_caption(
                caption=status_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        elif status_msg:
            await status_msg.edit_text(
                status_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True,
            )
    except Exception:
        try:
            status_msg = await context.bot.send_message(
                chat_id=pending.chat_id,
                text=status_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=pending.reply_to_message_id,
                disable_web_page_preview=True,
            )
        except Exception:
            status_msg = None

    message = None
    if query.message and query.message.reply_to_message:
        message = query.message.reply_to_message
    if message is None:
        message = update.effective_message

    await _process_url(
        update,
        context,
        url,
        index=index,
        total=total,
        message=message,
        status_msg=status_msg,
        user=user,
        max_height=height,
    )


async def _process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    index: int = 1,
    total: int = 1,
    message=None,
    status_msg=None,
    user=None,
    max_height: int | None = None,
) -> None:
    message = message or update.effective_message
    user = user or update.effective_user
    if not message or not user:
        return

    chat_id = message.chat_id
    name, emoji = msg.detect_platform(url)

    current_task = asyncio.current_task()
    job = register_job(user.id, url, task=current_task)
    cancel_check = job.cancel_check()

    # Instant path: re-send previously uploaded Telegram file_id — no status stages
    # Skip when user explicitly chose a quality.
    cached = None if max_height is not None else media_cache.get_cached(url)
    if cached:
        # Old bug stored Instagram reels as audio file_ids — those can't be
        # re-sent as video. Drop and re-download once.
        ig = "instagram.com" in url.lower() or "instagr.am" in url.lower()
        if ig and cached.is_audio:
            logger.warning(
                "Dropping bad Instagram audio cache for %s (file was stored as audio)",
                url[:80],
            )
            media_cache.delete_cached(url)
            cached = None

    if cached:
        logger.info("Cache hit for %s → file_id (skip stages)", url[:80])
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            if cached.is_album:
                from telegram import InputMediaPhoto, InputMediaVideo

                media_group = []
                for i, item in enumerate(cached.album_items or []):
                    fid = item.get("file_id")
                    if not fid:
                        continue
                    caption = _media_caption(
                        url,
                        MediaResult(
                            title=cached.title,
                            is_audio=False,
                            file_size=cached.file_size,
                            is_image=cached.is_image,
                        ),
                    ) if i == 0 else None
                    if item.get("kind") == "video":
                        media_group.append(
                            InputMediaVideo(
                                media=fid,
                                caption=caption,
                                parse_mode=ParseMode.HTML if caption else None,
                            )
                        )
                    else:
                        media_group.append(
                            InputMediaPhoto(
                                media=fid,
                                caption=caption,
                                parse_mode=ParseMode.HTML if caption else None,
                            )
                        )
                if not media_group:
                    raise BadRequest("Empty album cache")
                await message.reply_media_group(media=media_group[:10])
                result = MediaResult(
                    title=cached.title,
                    is_audio=False,
                    file_size=cached.file_size,
                    is_image=cached.is_image,
                )
            else:
                result = MediaResult(
                    title=cached.title,
                    is_audio=cached.is_audio,
                    file_size=cached.file_size,
                    telegram_file_id=cached.file_id,
                    is_image=cached.is_image,
                )
                await send_media(
                    message,
                    result,
                    _media_caption(url, result),
                    cancel_check=cancel_check,
                )
            if status_msg is not None:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            _record_success(update, url, result, user=user, message=message)
            unregister_job(user.id)
            return
        except (BadRequest, TelegramError) as exc:
            logger.warning("Cached file_id failed for %s: %s — re-downloading", url, exc)
            media_cache.delete_cached(url)

    # Cold path only: show Extracting / Uploading stages
    if status_msg is None:
        status_msg = await message.reply_text(
            msg.status_message(url, f"{emoji} <b>Extracting…</b>", index=index, total=total),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        try:
            await _edit_status(
                status_msg,
                url,
                f"{emoji} <b>Extracting…</b>",
                index=index,
                total=total,
            )
        except Exception:
            pass

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Quality picker for long YouTube / X / adult videos (2+ heights)
    if max_height is None and needs_quality_picker(url):
        shown = await _maybe_show_quality_picker(
            update,
            context,
            url,
            message=message,
            status_msg=status_msg,
            user=user,
            index=index,
            total=total,
            cancel_check=cancel_check,
        )
        if shown:
            unregister_job(user.id)
            return

    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue[str | None] = asyncio.Queue()
    progress_seen = asyncio.Event()
    worker_task = asyncio.create_task(
        _progress_worker(status_msg, url, progress_queue, index=index, total=total)
    )
    heartbeat_task = asyncio.create_task(
        _connecting_heartbeat(
            status_msg,
            url,
            name=name,
            emoji=emoji,
            index=index,
            total=total,
            progress_seen=progress_seen,
        )
    )

    def on_progress(text: str) -> None:
        progress_seen.set()
        loop.call_soon_threadsafe(progress_queue.put_nowait, text)

    result = None
    try:
        chosen_height = max_height
        result = await loop.run_in_executor(
            None,
            lambda h=chosen_height: resolve_media(
                url,
                progress_callback=on_progress,
                cancel_check=cancel_check,
                max_height=h,
            ),
        )

        progress_seen.set()
        await _stop_progress_worker(progress_queue, worker_task)
        await _stop_heartbeat(heartbeat_task)

        # Attribution caption: title, uploader, original link
        media_caption = _media_caption(url, result)

        if result.album:
            action = ChatAction.UPLOAD_PHOTO
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
            await _edit_status(
                status_msg,
                url,
                "📤 <b>Uploading…</b>",
                index=index,
                total=total,
            )
            file_id = await send_media(
                message, result, media_caption, cancel_check=cancel_check
            )
            _store_file_id(url, result, file_id)
            await status_msg.delete()
            _record_success(update, url, result, user=user, message=message)
            return

        if result.used_direct and result.direct_url:
            await _edit_status(
                status_msg,
                url,
                "📤 <b>Uploading…</b>",
                index=index,
                total=total,
            )
            try:
                file_id = await send_media(
                    message, result, media_caption, cancel_check=cancel_check
                )
                _store_file_id(url, result, file_id)
                await status_msg.delete()
                _record_success(update, url, result, user=user, message=message)
                return
            except (BadRequest, TelegramError) as exc:
                logger.warning(
                    "Direct CDN hotlink failed — falling back to VPS download+upload:\n"
                    "  page_url=%s\n"
                    "  title=%s\n"
                    "  is_audio=%s\n"
                    "  size=%s\n"
                    "  cdn_url=%s\n"
                    "  error_type=%s\n"
                    "  error=%s",
                    url,
                    result.title,
                    result.is_audio,
                    result.file_size,
                    (result.direct_url or "")[:300],
                    type(exc).__name__,
                    exc,
                )
                worker_task = asyncio.create_task(
                    _progress_worker(status_msg, url, progress_queue, index=index, total=total)
                )
                if result.cached_info:
                    dl_fn = lambda: download_from_info(
                        result.cached_info,
                        url,
                        progress_callback=on_progress,
                        cancel_check=cancel_check,
                    )
                else:
                    dl_fn = lambda: resolve_media(
                        url,
                        progress_callback=on_progress,
                        cancel_check=cancel_check,
                    )
                result = await loop.run_in_executor(None, dl_fn)
                await _stop_progress_worker(progress_queue, worker_task)
                media_caption = _media_caption(url, result)
        elif result.file_path or result.is_image:
            if result.is_image:
                action = ChatAction.UPLOAD_PHOTO
            elif result.is_audio:
                action = ChatAction.UPLOAD_DOCUMENT
            else:
                action = ChatAction.UPLOAD_VIDEO
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
            file_id = await _upload_media_with_progress(
                status_msg=status_msg,
                url=url,
                progress_queue=progress_queue,
                index=index,
                total=total,
                message=message,
                result=result,
                media_caption=media_caption,
                cancel_check=cancel_check,
            )
            _store_file_id(url, result, file_id)
            await status_msg.delete()
            _record_success(update, url, result, user=user, message=message)
            return

        action = ChatAction.UPLOAD_DOCUMENT if result.is_audio else ChatAction.UPLOAD_VIDEO
        await context.bot.send_chat_action(chat_id=chat_id, action=action)

        file_id = await _upload_media_with_progress(
            status_msg=status_msg,
            url=url,
            progress_queue=progress_queue,
            index=index,
            total=total,
            message=message,
            result=result,
            media_caption=media_caption,
            cancel_check=cancel_check,
        )
        _store_file_id(url, result, file_id)
        await status_msg.delete()
        _record_success(update, url, result, user=user, message=message)

    except DownloadCancelledError:
        logger.info("User %s cancelled download for %s", user.id, url)
        await _stop_progress_worker(progress_queue, worker_task)
        if result and result.file_path:
            cleanup_file(result.file_path)
        elif result and result.album:
            _cleanup_album(result)
        await status_msg.edit_text(
            msg.cancelled_message(url, index=index, total=total),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        raise
    except asyncio.CancelledError:
        logger.info("Task cancelled for user %s url %s", user.id, url)
        await _stop_progress_worker(progress_queue, worker_task)
        if result and result.file_path:
            cleanup_file(result.file_path)
        elif result and result.album:
            _cleanup_album(result)
        await status_msg.edit_text(
            msg.cancelled_message(url, index=index, total=total),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        raise DownloadCancelledError from None
    except FileTooLargeError as exc:
        logger.warning("File too large for %s: %s", url, exc)
        await _stop_progress_worker(progress_queue, worker_task)
        await status_msg.edit_text(
            msg.error_message(url, str(exc), index=index, total=total),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Failed to process %s", url)
        _record_failure(update, url, exc, user=user, message=message, kind="failed")
        await _stop_progress_worker(progress_queue, worker_task)
        await status_msg.edit_text(
            msg.error_message(url, msg.friendly_error(str(exc)), index=index, total=total),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    finally:
        progress_seen.set()
        await _stop_heartbeat(heartbeat_task)
        unregister_job(user.id)
        if (
            result
            and result.needs_cleanup
            and not job.cancel_event.is_set()
        ):
            if result.album:
                _cleanup_album(result)
            elif result.file_path and result.file_path.exists():
                cleanup_file(result.file_path)


def _cleanup_album(result) -> None:
    paths = [item.path for item in (result.album or []) if item.path]
    if not paths:
        return
    # All slides share one job directory
    cleanup_file(paths[0])


def _media_caption(url: str, result) -> str:
    album_count = len(result.album) if result.album else None
    uploader = getattr(result, "uploader", None) or _uploader_from_url(url)
    return msg.caption(
        result.title or "media",
        url,
        is_audio=bool(result.is_audio),
        is_image=bool(result.is_image),
        file_size=result.file_size,
        album_count=album_count,
        uploader=uploader,
    )


def _uploader_from_url(url: str) -> str | None:
    import re

    match = re.search(
        r"(?:tiktok\.com|instagram\.com|youtube\.com)/@([^/?#]+)",
        url,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _store_file_id(url: str, result, file_id) -> None:
    """Persist Telegram file_id(s) for instant re-send. Accepts str or album item list."""
    album_items = None
    single_id = None
    if isinstance(file_id, list):
        album_items = [
            item
            for item in file_id
            if isinstance(item, dict) and item.get("file_id")
        ]
        if len(album_items) == 1:
            single_id = album_items[0]["file_id"]
            album_items = None
        elif len(album_items) < 2:
            return
    elif isinstance(file_id, str) and file_id:
        single_id = file_id
    else:
        return

    media_cache.store_cached(
        url,
        single_id or (album_items[0]["file_id"] if album_items else ""),
        is_audio=result.is_audio,
        is_image=bool(result.is_image),
        title=result.title,
        file_size=result.file_size,
        album_items=album_items,
    )


def _record_success(update: Update, url: str, result, *, user=None, message=None) -> None:
    user = user or update.effective_user
    chat = update.effective_chat
    if message and message.chat:
        chat = message.chat
    if not user or not chat:
        return
    platform, _ = detect_platform(url)
    stats.record_download(
        user_id=user.id,
        username=user.username,
        chat_id=chat.id,
        chat_type=chat.type,
        url=url,
        platform=platform,
        file_size=result.file_size,
    )


def _record_failure(
    update: Update,
    url: str,
    exc: BaseException,
    *,
    user=None,
    message=None,
    kind: str = "failed",
) -> None:
    user = user or update.effective_user
    chat = update.effective_chat
    if message and message.chat:
        chat = message.chat
    platform, _ = detect_platform(url)
    try:
        stats.record_failure(
            kind=kind,
            url=url,
            error=str(exc),
            user_id=user.id if user else None,
            username=user.username if user else None,
            chat_id=chat.id if chat else None,
            chat_type=chat.type if chat else None,
            platform=platform,
        )
    except Exception:
        logger.exception("Failed to record download failure")


async def _upload_media_with_progress(
    *,
    status_msg,
    url: str,
    progress_queue: asyncio.Queue[str | None],
    index: int,
    total: int,
    message,
    result,
    media_caption: str,
    cancel_check=None,
) -> str | None:
    loop = asyncio.get_running_loop()
    upload_worker = asyncio.create_task(
        _progress_worker(status_msg, url, progress_queue, index=index, total=total)
    )
    on_upload = _make_progress_reporter(loop, progress_queue, msg.upload_progress)
    try:
        return await send_media(
            message,
            result,
            media_caption,
            progress_callback=on_upload,
            cancel_check=cancel_check,
        )
    finally:
        await _stop_progress_worker(progress_queue, upload_worker)


def _make_progress_reporter(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[str | None],
    formatter,
):
    state = {"text": "", "at": 0.0}

    def report(uploaded: int, total: int | None) -> None:
        text = formatter(uploaded, total)
        now = time.monotonic()
        if text == state["text"]:
            return
        if total and uploaded < total and now - state["at"] < 1.5:
            return
        state["text"] = text
        state["at"] = now
        loop.call_soon_threadsafe(queue.put_nowait, text)

    return report


async def _progress_worker(
    status_msg,
    url: str,
    queue: asyncio.Queue[str | None],
    *,
    index: int,
    total: int,
) -> None:
    while True:
        text = await queue.get()
        if text is None:
            break
        await _edit_status(
            status_msg,
            url,
            text,
            index=index,
            total=total,
        )


async def _stop_progress_worker(queue: asyncio.Queue[str | None], task: asyncio.Task) -> None:
    await queue.put(None)
    if not task.done():
        await task


async def _connecting_heartbeat(
    status_msg,
    url: str,
    *,
    name: str,
    emoji: str,
    index: int,
    total: int,
    progress_seen: asyncio.Event,
) -> None:
    """Update status while extract/connect is slow so users know the bot is working."""
    start = time.monotonic()
    phase = 0
    try:
        while not progress_seen.is_set():
            elapsed = int(time.monotonic() - start)
            try:
                text = msg.connecting_status(
                    url,
                    name=name,
                    emoji=emoji,
                    elapsed_sec=elapsed,
                    phase=phase,
                    index=index,
                    total=total,
                )
                if getattr(status_msg, "photo", None):
                    await status_msg.edit_caption(
                        caption=text,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await status_msg.edit_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
            except Exception:
                pass
            phase += 1
            try:
                await asyncio.wait_for(progress_seen.wait(), timeout=3.0)
                break
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass


async def _stop_heartbeat(task: asyncio.Task) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _edit_status(
    status_msg,
    url: str,
    step: str,
    *,
    index: int,
    total: int,
    title: str | None = None,
) -> None:
    if status_msg is None:
        return
    text = msg.status_message(url, step, index=index, total=total, title=title)
    try:
        if getattr(status_msg, "photo", None):
            await status_msg.edit_caption(
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await status_msg.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception:
        pass
