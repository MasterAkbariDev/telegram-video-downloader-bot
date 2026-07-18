"""Telegram message handlers."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update
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
    """Inline mode: @botname https://instagram.com/reel/…"""
    inline = update.inline_query
    raw_query = (inline.query or "").strip()
    query = inline_cache.strip_inline_query(raw_query)

    logger.info(
        "Inline query from user %s: raw=%r cleaned=%r",
        inline.from_user.id if inline.from_user else "?",
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
                        message_text="Paste a full URL after @bot — e.g. https://instagram.com/reel/…",
                    ),
                )
            ],
            cache_time=15,
            is_personal=True,
        )
        return

    urls = extract_urls(query)
    if not urls:
        await inline.answer(
            [
                InlineQueryResultArticle(
                    id="no-url",
                    title="No valid link found",
                    description="Include https:// — e.g. https://youtube.com/watch?v=…",
                    input_message_content=InputTextMessageContent(
                        message_text=query or raw_query,
                    ),
                )
            ],
            cache_time=5,
            is_personal=True,
        )
        return

    results = []
    for url in urls[:5]:
        name, emoji = msg.detect_platform(url)
        result_id = inline_cache.store_url(url)
        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=f"{emoji} Download from {name}",
                description=f"{msg.truncate_url(url, 56)} · video sent in this chat",
                input_message_content=InputTextMessageContent(
                    message_text=url,
                    disable_web_page_preview=True,
                ),
            )
        )

    await inline.answer(results, cache_time=5, is_personal=True)


async def chosen_inline_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log inline selections; target-chat message handler performs the download."""
    chosen = update.chosen_inline_result
    if not chosen or not chosen.from_user:
        return

    url = inline_cache.pop_url(chosen.result_id)
    if not url:
        query = inline_cache.strip_inline_query(chosen.query or "")
        found = extract_urls(query)
        url = found[0] if found else None

    logger.info(
        "Inline result chosen by user %s (result_id=%s url=%s) — awaiting message in target chat",
        chosen.from_user.id,
        chosen.result_id,
        (url or "")[:100],
    )
    if url:
        pending = inline_cache.register_pending(chosen.from_user.id, url)
        asyncio.create_task(_inline_target_chat_fallback(context, chosen.from_user.id, url, pending))


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
    inline_cache.mark_message_seen(update.effective_user.id if update.effective_user else None, urls)

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


async def _inline_target_chat_fallback(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    url: str,
    pending,
) -> None:
    """Tell the user why inline did not start when privacy hides the posted URL."""
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=4.0)
        return
    except asyncio.TimeoutError:
        pass

    logger.info(
        "Inline target-chat message was not received for user %s url %s — sending DM fallback",
        user_id,
        url[:100],
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ <b>Inline download could not start in that chat.</b>\n\n"
                "Telegram did not deliver the posted inline message to me. "
                "In groups this usually means bot privacy is still enabled.\n\n"
                "Fix in @BotFather:\n"
                "1. <code>/setprivacy</code> → Disable\n"
                "2. Remove and re-add the bot to the group\n\n"
                "Or paste the link directly in the chat.\n\n"
                f"<code>{msg.esc(msg.truncate_url(url, 90))}</code>"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.warning("Could not send inline fallback DM to user %s: %s", user_id, exc)


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

    current_task = asyncio.current_task()
    job = register_job(user.id, url, task=current_task)
    cancel_check = job.cancel_check()

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Instant path: re-send previously uploaded Telegram file_id
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
        logger.info("Cache hit for %s → file_id", url[:80])
        try:
            await status_msg.edit_text(
                msg.status_message(url, "📤 <b>Uploading…</b>", index=index, total=total),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
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
            await status_msg.delete()
            _record_success(update, url, result, user=user, message=message)
            return
        except (BadRequest, TelegramError) as exc:
            logger.warning("Cached file_id failed for %s: %s — re-downloading", url, exc)
            media_cache.delete_cached(url)

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


def _store_file_id(url: str, result, file_id: str | None) -> None:
    if not file_id:
        return
    # Don't cache multi-image albums (single file_id isn't enough)
    if result.album and len(result.album) > 1:
        return
    media_cache.store_cached(
        url,
        file_id,
        is_audio=result.is_audio,
        is_image=bool(result.is_image),
        title=result.title,
        file_size=result.file_size,
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
