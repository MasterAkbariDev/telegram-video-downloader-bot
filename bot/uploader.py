"""Upload media via Bot API (50 MB) or Telethon MTProto (up to 2 GB)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from telegram import InputFile, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from bot.config import (
    BOT_TOKEN,
    LARGE_UPLOAD_LIMIT,
    STANDARD_UPLOAD_LIMIT,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELETHON_SESSION,
    large_upload_enabled,
)
from bot.downloader import MediaResult

try:
    from bot.jobs import CancelCheck, DownloadCancelledError
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]
    DownloadCancelledError = RuntimeError

logger = logging.getLogger(__name__)

UploadProgressCallback = Callable[[int, int | None], None]

# Bot API reads in small chunks — use 512 KB for smoother throughput
_BOT_API_READ_BYTES = 512 * 1024
# Telethon max part size (512 KB) — parallel MTProto upload
_TELETHON_PART_SIZE_KB = 512

_telethon_client = None
_telethon_lock = asyncio.Lock()


class _ProgressFile:
    """File wrapper that reports bytes read during upload."""

    def __init__(
        self,
        path: Path,
        callback: UploadProgressCallback | None,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        self._path = path
        self._file = path.open("rb")
        self._size = path.stat().st_size
        self._callback = callback
        self._cancel_check = cancel_check
        self.name = str(path)

    def read(self, size: int = -1) -> bytes:
        if self._cancel_check:
            self._cancel_check()
        if size < 0 or size > _BOT_API_READ_BYTES:
            size = _BOT_API_READ_BYTES
        data = self._file.read(size)
        if data and self._callback:
            self._callback(self._file.tell(), self._size)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> _ProgressFile:
        return self

    def __exit__(self, *args) -> None:
        self.close()


async def reset_telethon_client() -> None:
    """Disconnect after API credentials change."""
    global _telethon_client
    async with _telethon_lock:
        if _telethon_client is not None:
            try:
                await _telethon_client.disconnect()
            except Exception:
                pass
            _telethon_client = None


async def _get_telethon():
    if not large_upload_enabled():
        return None

    global _telethon_client
    async with _telethon_lock:
        if _telethon_client is not None and _telethon_client.is_connected():
            return _telethon_client

        from telethon import TelegramClient

        session = str(TELETHON_SESSION)
        _telethon_client = TelegramClient(
            session,
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH,
            connection_retries=5,
        )
        await _telethon_client.start(bot_token=BOT_TOKEN)
        logger.info("Telethon MTProto client ready (2 GB upload mode)")
        return _telethon_client


def upload_limit_label() -> str:
    return "2 GB" if large_upload_enabled() else "50 MB"


async def send_media(
    message: Message,
    result: MediaResult,
    caption: str,
    *,
    progress_callback: UploadProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> str | None:
    """Send video/audio/photo/album as a reply. Returns Telegram file_id when available."""
    parse = ParseMode.HTML if caption else None

    if result.album and len(result.album) > 0:
        return await _send_album(message, result, caption)

    if result.is_image and (result.file_path or result.direct_url or result.telegram_file_id):
        return await _send_photo(message, result, caption)

    as_audio = _send_as_audio(result)
    if as_audio != result.is_audio:
        logger.warning(
            "Overriding is_audio=%s → %s (ext/path looks like video): path=%s url=%s",
            result.is_audio,
            as_audio,
            result.file_path,
            (result.direct_url or "")[:120],
        )
        result.is_audio = as_audio

    logger.info(
        "Upload start: as_audio=%s path=%s size=%s direct=%s file_id=%s",
        as_audio,
        result.file_path.name if result.file_path else None,
        result.file_size,
        bool(result.direct_url),
        bool(result.telegram_file_id),
    )

    if result.telegram_file_id:
        return await _send_via_file_id(message, result, caption, parse)

    if result.direct_url:
        try:
            sent = await _send_via_bot_api_url(message, result, caption, parse)
            return _file_id_from_message(sent, result.is_audio)
        except (BadRequest, TelegramError) as exc:
            logger.warning(
                "CDN/direct URL send failed (Telegram rejected hotlink):\n"
                "  title=%s\n"
                "  is_audio=%s\n"
                "  size=%s\n"
                "  cdn_host=%s\n"
                "  cdn_url=%s\n"
                "  error_type=%s\n"
                "  error=%s",
                result.title,
                result.is_audio,
                result.file_size,
                _url_host(result.direct_url),
                _short_url(result.direct_url),
                type(exc).__name__,
                exc,
            )
            raise

    if not result.file_path:
        raise RuntimeError("No media file or URL available to send.")

    file_size = result.file_size or result.file_path.stat().st_size
    if progress_callback:
        progress_callback(0, file_size)

    # Bot API is much faster for normal sizes (Instagram reels ~10–50 MB).
    # Telethon MTProto only helps (and is needed) above the 50 MB Bot API limit.
    use_telethon = large_upload_enabled() and file_size > STANDARD_UPLOAD_LIMIT

    if use_telethon:
        logger.info("Uploading via Telethon MTProto (%s)", _human_size(file_size))
        await _send_via_telethon(
            message,
            result,
            caption,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        return None

    if file_size > STANDARD_UPLOAD_LIMIT:
        raise RuntimeError(
            f"File is {_human_size(file_size)} but upload limit is 50 MB. "
            "Configure API ID + API Hash in admin settings for 2 GB uploads."
        )

    logger.info("Uploading via Bot API file (%s as_%s)", _human_size(file_size), "audio" if as_audio else "video")
    sent = await _send_via_bot_api_file(
        message,
        result,
        caption,
        parse,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return _file_id_from_message(sent, result.is_audio)


async def _send_photo(message: Message, result: MediaResult, caption: str = "") -> str | None:
    """Send image as a normal Telegram photo, always as a reply."""
    kwargs: dict = {}
    if caption:
        kwargs["caption"] = caption
        kwargs["parse_mode"] = ParseMode.HTML
    opened = None
    try:
        if result.telegram_file_id:
            sent = await message.reply_photo(photo=result.telegram_file_id, **kwargs)
            return _file_id_from_message(sent, is_audio=False, is_photo=True)
        if result.file_path:
            opened = result.file_path.open("rb")
            media = InputFile(opened, filename=result.file_path.name)
            logger.info("Uploading photo (%s)", result.file_path.name)
            sent = await message.reply_photo(photo=media, **kwargs)
            return _file_id_from_message(sent, is_audio=False, is_photo=True)
        if result.direct_url:
            sent = await message.reply_photo(photo=result.direct_url, **kwargs)
            return _file_id_from_message(sent, is_audio=False, is_photo=True)
        raise RuntimeError("No photo to send.")
    finally:
        if opened:
            opened.close()


async def _send_album(
    message: Message,
    result: MediaResult,
    caption: str = "",
) -> str | None:
    album = result.album or []
    if len(album) == 1:
        only = album[0]
        single = MediaResult(
            title=result.title,
            is_audio=False,
            file_size=only.file_size,
            file_path=only.path,
            direct_url=only.url if not only.path else None,
            is_image=only.kind == "image",
            uploader=result.uploader,
        )
        if only.kind == "image":
            return await _send_photo(message, single, caption)
        return await send_media(message, single, caption)

    logger.info("Uploading media group (%d items, reply)", len(album))
    media_group = _build_album_media(album, caption=caption)
    if not media_group:
        raise RuntimeError("No album items to send.")

    try:
        sent_list = await message.reply_media_group(media=media_group[:10])
    except BadRequest as exc:
        # Caption / parse quirks → retry bare album, then caption as reply
        logger.warning("Album send failed (%s) — retrying without caption", exc)
        bare = _build_album_media(album, caption="")
        if not bare:
            raise
        sent_list = await message.reply_media_group(media=bare[:10])
        if caption:
            try:
                await message.reply_text(
                    caption,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError as cap_exc:
                logger.warning("Could not send album caption separately: %s", cap_exc)

    if sent_list:
        return _file_id_from_message(sent_list[0], is_audio=False, is_photo=True)
    return None


def _build_album_media(album: list, *, caption: str = "") -> list:
    """Build InputMedia list. Pass raw bytes so PTB assigns attach:// names."""
    from telegram import InputMediaPhoto, InputMediaVideo

    media_group = []
    for index, item in enumerate(album):
        cap_kwargs: dict = {}
        if index == 0 and caption:
            cap_kwargs["caption"] = caption
            cap_kwargs["parse_mode"] = ParseMode.HTML

        media = None
        filename = None
        if item.path and item.path.is_file() and item.path.stat().st_size > 0:
            # Pass bytes (not Path, not pre-built InputFile without attach=True).
            # Path → file:// (cloud API rejects). InputFile without attach → media not found.
            media = item.path.read_bytes()
            filename = item.path.name
        elif item.url and str(item.url).startswith(("http://", "https://")):
            media = item.url
        if not media:
            logger.warning("Skipping empty/invalid album item %s", index)
            continue

        if item.kind == "video":
            media_group.append(
                InputMediaVideo(
                    media=media,
                    filename=filename,
                    supports_streaming=True,
                    **cap_kwargs,
                )
            )
        else:
            media_group.append(
                InputMediaPhoto(
                    media=media,
                    filename=filename,
                    **cap_kwargs,
                )
            )
    return media_group


_VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}


def _send_as_audio(result: MediaResult) -> bool:
    """Hard guard: mp4/webm/etc. must never use reply_audio (Telegram audio UI)."""
    if result.file_path:
        suffix = result.file_path.suffix.lower()
        if suffix in _VIDEO_SUFFIXES:
            return False
        if suffix in _AUDIO_SUFFIXES:
            return True

    if result.direct_url:
        lower = result.direct_url.lower().split("?", 1)[0]
        if any(lower.endswith(ext) for ext in _VIDEO_SUFFIXES):
            return False
        if any(lower.endswith(ext) for ext in _AUDIO_SUFFIXES):
            return True
        # Instagram / CDN progressive video URLs often have no extension
        if any(token in lower for token in ("video", "reel", ".mp4", "mime=video")):
            return False

    return bool(result.is_audio)


def _url_host(url: str | None) -> str:
    if not url:
        return "-"
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or "-"
    except Exception:
        return "-"


def _short_url(url: str | None, keep: int = 160) -> str:
    if not url:
        return "-"
    if len(url) <= keep:
        return url
    return f"{url[: keep // 2]}…{url[-(keep // 2) :]}"


async def _send_via_file_id(message: Message, result: MediaResult, caption: str, parse) -> str:
    kwargs = {}
    if caption:
        kwargs["caption"] = caption
        if parse:
            kwargs["parse_mode"] = parse
    if result.is_audio:
        sent = await message.reply_audio(
            audio=result.telegram_file_id,
            title=result.title[:64],
            **kwargs,
        )
    else:
        sent = await message.reply_video(
            video=result.telegram_file_id,
            supports_streaming=True,
            **kwargs,
        )
    return result.telegram_file_id or _file_id_from_message(sent, result.is_audio) or ""


async def _send_via_bot_api_url(message: Message, result: MediaResult, caption: str, parse):
    kwargs = {}
    if caption:
        kwargs["caption"] = caption
        if parse:
            kwargs["parse_mode"] = parse
    if result.is_audio:
        return await message.reply_audio(
            audio=result.direct_url,
            title=result.title[:64],
            **kwargs,
        )
    return await message.reply_video(
        video=result.direct_url,
        supports_streaming=True,
        **kwargs,
    )


async def _send_via_bot_api_file(
    message: Message,
    result: MediaResult,
    caption: str,
    parse,
    *,
    progress_callback: UploadProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    kwargs = {}
    if caption:
        kwargs["caption"] = caption
        if parse:
            kwargs["parse_mode"] = parse
    try:
        with _ProgressFile(result.file_path, progress_callback, cancel_check) as media_file:
            upload = InputFile(
                media_file,
                filename=result.file_path.name,
                read_file_handle=False,
            )
            if result.is_audio:
                return await message.reply_audio(
                    audio=upload,
                    title=result.title[:64],
                    **kwargs,
                )
            return await message.reply_video(
                video=upload,
                supports_streaming=True,
                **kwargs,
            )
    except (BadRequest, TelegramError) as exc:
        if large_upload_enabled() and result.file_path:
            logger.warning("Bot API upload failed, trying Telethon: %s", exc)
            await _send_via_telethon(
                message,
                result,
                caption,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            return None
        raise


def _file_id_from_message(sent, is_audio: bool, *, is_photo: bool = False) -> str | None:
    if sent is None:
        return None
    try:
        if is_audio and sent.audio:
            return sent.audio.file_id
        if is_photo and sent.photo:
            return sent.photo[-1].file_id
        if sent.photo:
            return sent.photo[-1].file_id
        if sent.video:
            return sent.video.file_id
        if sent.document:
            return sent.document.file_id
    except Exception:
        return None
    return None


async def _send_via_telethon(
    message: Message,
    result: MediaResult,
    caption: str,
    *,
    progress_callback: UploadProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    client = await _get_telethon()
    if client is None:
        raise RuntimeError("2 GB upload mode is not configured.")

    if result.file_path.stat().st_size > LARGE_UPLOAD_LIMIT:
        raise RuntimeError("File exceeds the 2 GB Telegram limit.")

    def on_progress(current: int, total: int) -> None:
        if cancel_check:
            cancel_check()
        if progress_callback:
            progress_callback(current, total)

    await client.send_file(
        message.chat_id,
        str(result.file_path),
        caption=caption or None,
        parse_mode="html" if caption else None,
        force_document=result.is_audio,
        supports_streaming=not result.is_audio,
        reply_to=message.message_id,
        progress_callback=on_progress,
        part_size_kb=_TELETHON_PART_SIZE_KB,
    )


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
