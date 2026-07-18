"""Media download logic powered by yt-dlp."""

from __future__ import annotations

import copy
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadError

from bot.config import (
    DOWNLOAD_DIR,
    INSTAGRAM_MIN_INTERVAL,
    MAX_VIDEO_HEIGHT,
    STANDARD_UPLOAD_LIMIT,
    YTDLP_PROXY,
    get_cookies_file,
    get_download_size_cap,
    get_max_file_size,
    get_max_filesize_label,
    large_upload_enabled,
)
from bot.messages import detect_platform
from bot.fast_download import (
    download_http,
    make_client,
    ytdlp_http_candidate,
    ytdlp_http_headers,
)

try:
    from bot.jobs import CancelCheck, DownloadCancelledError
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]
    DownloadCancelledError = RuntimeError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_DIRECT_URL_SKIP = (
    "youtube.com",
    "youtu.be",
    "spotify.com",
    "open.spotify.com",
    "music.apple.com",
    "deezer.com",
    # TikTok CDN needs session cookies Telegram cannot send
    "tiktok.com",
)

_INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")
_TIKTOK_HOSTS = ("tiktok.com",)

_instagram_lock = threading.Lock()
_instagram_ydl: yt_dlp.YoutubeDL | None = None
_instagram_ydl_key: tuple | None = None
_last_instagram_request = 0.0

_RETRY_DELAYS = (2, 5, 10, 20)


class FileTooLargeError(RuntimeError):
    """Raised when media exceeds Telegram's upload limit."""

    def __init__(self, size: int | None, *, tried_lower_quality: bool = False) -> None:
        self.size = size
        self.tried_lower_quality = tried_lower_quality
        if size:
            super().__init__(
                f"This file is {_format_size(size)} — Telegram bots can only send up to "
                f"{_format_size(get_max_file_size())}. Try a shorter clip."
            )
        else:
            super().__init__(
                f"This file exceeds Telegram's {_format_size(get_max_file_size())} limit. "
                "Try a shorter clip or lower-quality source."
            )


@dataclass
class AlbumItem:
    kind: str  # "image" | "video"
    path: Path | None = None
    url: str | None = None
    file_size: int | None = None


@dataclass
class MediaResult:
    title: str
    is_audio: bool
    file_size: int | None
    direct_url: str | None = None
    file_path: Path | None = None
    used_direct: bool = False
    cached_info: dict | None = None
    telegram_file_id: str | None = None
    is_image: bool = False
    album: list[AlbumItem] | None = None
    uploader: str | None = None

    @property
    def needs_cleanup(self) -> bool:
        if self.file_path is not None:
            return True
        if self.album:
            return any(item.path is not None for item in self.album)
        return False


def extract_urls(text: str) -> list[str]:
    """Extract supported URLs from plain text."""
    from bot.urls import extract_urls as _extract

    return _extract(text)


def resolve_media(
    url: str,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    *,
    force_audio: bool = False,
    display_title: str | None = None,
) -> MediaResult:
    """Single-pass: extract once, then direct-send or download (no double fetch)."""
    # Spotify is DRM in yt-dlp — resolve via SoundCloud / mirrors instead
    if not force_audio and not url.startswith(("ytsearch", "scsearch")):
        from bot.spotify import (
            is_spotify_url,
            is_youtube_playlist_url,
            resolve_spotify_via_youtube,
            resolve_youtube_playlist_audio,
        )
        from bot.instagram import is_instagram_url, resolve_instagram_album

        if is_spotify_url(url):
            return resolve_spotify_via_youtube(
                url,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        if is_youtube_playlist_url(url):
            return resolve_youtube_playlist_audio(
                url,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        # Instagram photos / carousels (yt-dlp is video-only)
        if is_instagram_url(url):
            album = resolve_instagram_album(
                url,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            if album:
                return album

    # TikTok CDN cookies are bound to the extract session — download in one pass
    if _is_tiktok_url(url) and not force_audio:
        return _resolve_tiktok(
            url,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            display_title=display_title,
        )

    audio_preferred = force_audio or _is_audio_url(url)
    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if cancel_check:
            cancel_check()
        if progress_callback:
            name, emoji = detect_platform(url)
            progress_callback(f"{emoji} <b>Extracting…</b>")

        if cancel_check:
            cancel_check()

        info, opts = _extract_with_size_limit(
            url,
            audio_preferred=audio_preferred,
            output_dir=output_dir,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

        title = display_title or info.get("title") or info.get("id") or "media"
        is_audio = _result_is_audio(info, url=url, audio_preferred=audio_preferred)

        # Instagram: always try a progressive CDN URL so Telegram fetches it (no VPS upload)
        if not force_audio:
            direct_url = _direct_url_from_info(info, prefer_small=_is_instagram_url(url))
            if direct_url and not _should_skip_direct_url(url):
                est = _estimate_size(info)
                logger.info(
                    "Trying CDN direct send for %s (size≈%s, is_audio=%s, cdn=%s)",
                    url,
                    _format_size(est) if est else "?",
                    is_audio,
                    (direct_url[:120] + "…") if len(direct_url) > 120 else direct_url,
                )
                return MediaResult(
                    title=title,
                    is_audio=is_audio,
                    file_size=_estimate_size(info),
                    direct_url=direct_url,
                    used_direct=True,
                    cached_info=copy.deepcopy(info),
                    uploader=_uploader_from_info(info),
                )

        if progress_callback:
            est = _estimate_size(info)
            if est:
                progress_callback(
                    f"⬇️ <b>Downloading…</b> ({_format_size(est)} / {_format_size(get_max_file_size())} max)"
                )
            else:
                progress_callback("⬇️ <b>Starting download…</b>")

        if cancel_check:
            cancel_check()

        # TikTok CDN requires extract-session cookies — use yt-dlp's downloader.
        fast = None if _is_tiktok_url(url) else ytdlp_http_candidate(info)
        if fast:
            media_url, ext = fast
            dest = output_dir / f"download.{ext}"
            logger.info("Fast parallel HTTP download for %s", url)
            with make_client() as client:
                download_http(
                    client,
                    media_url,
                    dest,
                    referer=url,
                    extra_headers=ytdlp_http_headers(info),
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    skip_probe=_is_instagram_url(url),
                )
            downloaded = dest
        else:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.process_info(copy.deepcopy(info))
            downloaded = _find_downloaded_file(output_dir)

        if not downloaded:
            raise RuntimeError("Download finished but no file was found.")

        is_audio = _result_is_audio(
            info, url=url, audio_preferred=audio_preferred, path=downloaded
        )

        downloaded = _maybe_compress(
            downloaded,
            is_audio=is_audio,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            source_url=url,
        )
        file_size = downloaded.stat().st_size
        if file_size > get_max_file_size():
            raise FileTooLargeError(file_size, tried_lower_quality=True)

        return MediaResult(
            title=title,
            is_audio=is_audio,
            file_size=file_size,
            file_path=downloaded,
            used_direct=False,
            uploader=_uploader_from_info(info),
        )
    except DownloadCancelledError:
        _cleanup_job_dir(output_dir)
        raise
    except FileTooLargeError:
        _cleanup_job_dir(output_dir)
        raise
    except Exception as exc:
        _cleanup_job_dir(output_dir)
        logger.warning("yt-dlp failed for %s: %s — trying fallback", url, exc)

        # Instagram image posts often fail yt-dlp ("no video formats")
        if _is_instagram_url(url) and not force_audio:
            try:
                from bot.instagram import resolve_instagram_album

                album = resolve_instagram_album(
                    url,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
                if album:
                    return album
            except Exception as ig_exc:
                logger.warning("Instagram album fallback failed: %s", ig_exc)

        job_id = uuid.uuid4().hex[:12]
        fallback_dir = DOWNLOAD_DIR / job_id
        fallback_dir.mkdir(parents=True, exist_ok=True)
        try:
            if progress_callback:
                progress_callback("🔄 <b>Trying backup downloader…</b>")
            from bot.fallback import fallback_resolve

            result = fallback_resolve(
                url,
                fallback_dir,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            if result.file_path and not result.is_audio and not result.album:
                result.file_path = _maybe_compress(
                    result.file_path,
                    is_audio=False,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    source_url=url,
                )
                result.file_size = result.file_path.stat().st_size
            return result
        except Exception as fb_exc:
            _cleanup_job_dir(fallback_dir)
            logger.warning("Fallback failed for %s: %s", url, fb_exc)
            if is_youtube_bot_check(exc):
                logger.warning(
                    "YouTube bot-check (cookies=%s) for %s",
                    get_cookies_file() or "none",
                    url,
                )
                raise RuntimeError(youtube_bot_check_hint()) from exc
            raise exc


def download_from_info(
    cached_info: dict,
    url: str,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> MediaResult:
    """Download using a prior extract_info result — avoids slow re-lookup."""
    # TikTok CDN URLs die without the original session cookies
    if _is_tiktok_url(url):
        return resolve_media(
            url,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    audio_preferred = _is_audio_url(url)
    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if progress_callback:
        progress_callback("⬇️ <b>Downloading…</b>")

    opts = _build_ydl_opts(
        audio_preferred=audio_preferred,
        output_dir=output_dir,
        progress_callback=progress_callback,
        max_height=_height_from_info(cached_info),
        url=url,
        cancel_check=cancel_check,
    )

    title = cached_info.get("title") or cached_info.get("id") or "media"
    is_audio = _result_is_audio(cached_info, url=url, audio_preferred=audio_preferred)

    if cancel_check:
        cancel_check()

    fast = None if _is_tiktok_url(url) else ytdlp_http_candidate(cached_info)
    if fast:
        media_url, ext = fast
        dest = output_dir / f"download.{ext}"
        with make_client() as client:
            download_http(
                client,
                media_url,
                dest,
                referer=url,
                extra_headers=ytdlp_http_headers(cached_info),
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                skip_probe=_is_instagram_url(url),
            )
        downloaded = dest
    else:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_info(copy.deepcopy(cached_info))
        downloaded = _find_downloaded_file(output_dir)

    if not downloaded:
        raise RuntimeError("Download finished but no file was found.")

    is_audio = _result_is_audio(
        cached_info, url=url, audio_preferred=audio_preferred, path=downloaded
    )

    downloaded = _maybe_compress(
        downloaded,
        is_audio=is_audio,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        source_url=url,
    )
    file_size = downloaded.stat().st_size
    if file_size > get_max_file_size():
        cleanup_file(downloaded)
        raise FileTooLargeError(file_size, tried_lower_quality=True)

    return MediaResult(
        title=title,
        is_audio=is_audio,
        file_size=file_size,
        file_path=downloaded,
        used_direct=False,
        uploader=_uploader_from_info(cached_info),
    )


def download_media(
    url: str,
    progress_callback: ProgressCallback | None = None,
) -> MediaResult:
    """Full resolve fallback when no cached info is available."""
    return resolve_media(url, progress_callback)


def _resolve_tiktok(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    display_title: str | None = None,
) -> MediaResult:
    """
    Extract and download TikTok in one yt-dlp session.

    CDN URLs require cookies set during extraction; a separate process_info
    on a new YoutubeDL instance gets HTTP 403.
    """
    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if cancel_check:
            cancel_check()
        if progress_callback:
            name, emoji = detect_platform(url)
            progress_callback(f"{emoji} <b>Downloading…</b>")

        last_exc: BaseException | None = None
        for height in _height_fallbacks():
            if cancel_check:
                cancel_check()
            opts = _build_ydl_opts(
                audio_preferred=False,
                output_dir=output_dir,
                progress_callback=progress_callback,
                max_height=height,
                url=url,
                cancel_check=cancel_check,
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except DownloadError as exc:
                last_exc = exc
                if _is_format_unavailable(exc):
                    logger.warning("TikTok format retry at %dp: %s", height, exc)
                    continue
                raise

            if info is None:
                raise RuntimeError("Could not extract media information from this link.")
            info = _unwrap_search_or_single_entry(info, url)

            downloaded = _find_downloaded_file(output_dir)
            if not downloaded:
                raise RuntimeError("Download finished but no file was found.")

            title = display_title or info.get("title") or info.get("id") or "media"
            is_audio = False
            downloaded = _maybe_compress(
                downloaded,
                is_audio=is_audio,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                source_url=url,
            )
            file_size = downloaded.stat().st_size
            if file_size > get_max_file_size():
                downloaded.unlink(missing_ok=True)
                logger.info(
                    "TikTok file %s at %dp — trying lower quality",
                    _format_size(file_size),
                    height,
                )
                if progress_callback:
                    progress_callback(
                        f"⚠️ File is {_format_size(file_size)} "
                        f"(max {_format_size(get_max_file_size())}). "
                        f"<b>Trying lower quality…</b>"
                    )
                continue

            return MediaResult(
                title=title,
                is_audio=is_audio,
                file_size=file_size,
                file_path=downloaded,
                used_direct=False,
                uploader=_uploader_from_info(info),
            )

        if last_exc:
            raise last_exc
        raise FileTooLargeError(None, tried_lower_quality=True)
    except DownloadCancelledError:
        _cleanup_job_dir(output_dir)
        raise
    except FileTooLargeError:
        _cleanup_job_dir(output_dir)
        raise
    except Exception as exc:
        _cleanup_job_dir(output_dir)
        logger.warning("yt-dlp failed for %s: %s — trying fallback", url, exc)
        job_id = uuid.uuid4().hex[:12]
        fallback_dir = DOWNLOAD_DIR / job_id
        fallback_dir.mkdir(parents=True, exist_ok=True)
        try:
            if progress_callback:
                progress_callback("🔄 <b>Trying backup downloader…</b>")
            from bot.fallback import fallback_resolve

            result = fallback_resolve(
                url,
                fallback_dir,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
            if display_title:
                result.title = display_title
            return result
        except Exception as fb_exc:
            _cleanup_job_dir(fallback_dir)
            logger.warning("Fallback failed for %s: %s", url, fb_exc)
            raise exc


def _extract_with_size_limit(
    url: str,
    *,
    audio_preferred: bool,
    output_dir: Path,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None = None,
) -> tuple[dict, dict]:
    """Extract metadata, auto-lowering quality if the file exceeds 50 MB."""
    tried_lower = False
    heights = _height_fallbacks() if not audio_preferred else [MAX_VIDEO_HEIGHT]

    last_info: dict | None = None
    last_opts: dict | None = None

    for height in heights:
        opts = _build_ydl_opts(
            audio_preferred=audio_preferred,
            output_dir=output_dir,
            progress_callback=progress_callback,
            max_height=height,
            url=url,
            cancel_check=cancel_check,
        )
        try:
            info = _extract_info(
                url,
                opts,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        except DownloadError as exc:
            if not _is_format_unavailable(exc):
                raise
            info = None
            for fallback_fmt in _format_fallback_chain(height, url, audio_preferred=audio_preferred):
                if fallback_fmt == opts.get("format"):
                    continue
                logger.warning("Format retry for %s: %s", url, fallback_fmt)
                opts = {**opts, "format": fallback_fmt}
                try:
                    info = _extract_info(
                        url,
                        opts,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                    break
                except DownloadError as retry_exc:
                    if not _is_format_unavailable(retry_exc):
                        raise
                    logger.warning("Format still unavailable (%s): %s", fallback_fmt, retry_exc)
            if info is None:
                # Last resort: any format yt-dlp can see
                opts = {**opts, "format": "best/bestvideo+bestaudio/bestaudio"}
                logger.warning("Final format fallback for %s: %s", url, opts["format"])
                try:
                    info = _extract_info(
                        url,
                        opts,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                except DownloadError:
                    raise exc

        if info is None:
            raise RuntimeError("Could not extract media information from this link.")
        info = _unwrap_search_or_single_entry(info, url)

        last_info = info
        last_opts = opts
        est = _estimate_size(info)

        if est is None or est <= get_download_size_cap():
            return info, opts

        tried_lower = True
        logger.info("Estimated %s for %s at %dp — trying lower quality", _format_size(est), url, height)
        if progress_callback:
            progress_callback(
                f"⚠️ File is ~{_format_size(est)} (max {_format_size(get_max_file_size())}). "
                f"<b>Trying lower quality…</b>"
            )

    assert last_info is not None and last_opts is not None
    est = _estimate_size(last_info)
    raise FileTooLargeError(est, tried_lower_quality=tried_lower)


def _estimate_size(info: dict) -> int | None:
    """Best-effort size estimate from yt-dlp metadata."""
    if info.get("requested_formats"):
        total = 0
        for fmt in info["requested_formats"]:
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if size:
                total += size
            else:
                return None
        return total or None

    return info.get("filesize") or info.get("filesize_approx")


def _unwrap_search_or_single_entry(info: dict, url: str) -> dict:
    """ytsearch/scsearch return a playlist-shaped result — take the first usable hit."""
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        if info.get("entries") is not None:
            raise RuntimeError("No results found for this search.")
        return info

    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    is_search = (
        url.startswith(("ytsearch", "scsearch", "gdsearch", "yscsearch"))
        or "search" in extractor
        or (
            info.get("_type") == "playlist"
            and len(entries) >= 1
            and url.startswith(("ytsearch", "scsearch"))
        )
    )
    if is_search or len(entries) == 1:
        # Skip obvious ads when taking a generic first hit
        chosen = entries[0]
        for entry in entries:
            title = (entry.get("title") or "").lower()
            if re.search(r"\b(advert|advertisement|sponsored|promo)\b", title):
                continue
            duration = entry.get("duration")
            try:
                if duration is not None and float(duration) < 35 and "ad" in title:
                    continue
            except (TypeError, ValueError):
                pass
            chosen = entry
            break
        logger.info(
            "Using search entry for %s → %s",
            url[:80],
            chosen.get("title") or chosen.get("id") or chosen.get("url"),
        )
        return chosen

    raise RuntimeError(
        "Playlists are not supported via this link type — send a single track/video link."
    )


def _height_from_info(info: dict) -> int:
    height = info.get("height")
    if height:
        return int(height)
    for fmt in info.get("requested_formats") or []:
        if fmt.get("height"):
            return int(fmt["height"])
    return MAX_VIDEO_HEIGHT


def _height_fallbacks() -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    if large_upload_enabled():
        candidates = (1080, MAX_VIDEO_HEIGHT, 720, 480, 360)
    else:
        candidates = (MAX_VIDEO_HEIGHT, 360, 240, 144)
    for h in candidates:
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def _should_skip_direct_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in _DIRECT_URL_SKIP)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _is_instagram_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in _INSTAGRAM_HOSTS)


def _is_tiktok_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in _TIKTOK_HOSTS)


def _uploader_from_info(info: dict | None) -> str | None:
    if not info:
        return None
    for key in ("uploader", "creator", "channel", "artist", "uploader_id"):
        val = info.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _maybe_impersonate(opts: dict, url: str | None) -> None:
    """TikTok requires TLS fingerprint impersonation (curl_cffi)."""
    if not url or not _is_tiktok_url(url):
        return
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception as exc:
        logger.warning("TikTok impersonate unavailable (install curl_cffi): %s", exc)


def _instagram_throttle() -> None:
    global _last_instagram_request
    now = time.monotonic()
    wait = INSTAGRAM_MIN_INTERVAL - (now - _last_instagram_request)
    if wait > 0:
        time.sleep(wait)
    _last_instagram_request = time.monotonic()


def _extract_cache_key(opts: dict) -> tuple:
    return (
        opts.get("format"),
        opts.get("cookiefile"),
        opts.get("proxy"),
    )


def _extract_info_with_retry(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    last_exc: DownloadError | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS):
        if cancel_check:
            cancel_check()
        try:
            return ydl.extract_info(url, download=False)
        except DownloadError as exc:
            last_exc = exc
            msg = str(exc).lower()
            if "429" not in msg and "too many requests" not in msg:
                raise
            if attempt >= len(_RETRY_DELAYS) - 1:
                break
            logger.warning("Rate limited on %s — retry in %ss", url, delay)
            if progress_callback:
                progress_callback(f"⏳ <b>Rate limited — retrying in {delay}s…</b>")
            _sleep_with_cancel(delay, cancel_check)
    assert last_exc is not None
    raise last_exc


def _extract_info(
    url: str,
    opts: dict,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    if _is_instagram_url(url):
        return _instagram_extract(
            url,
            opts,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
    with yt_dlp.YoutubeDL(opts) as ydl:
        return _extract_info_with_retry(
            ydl,
            url,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )


def _instagram_extract(
    url: str,
    opts: dict,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    global _instagram_ydl, _instagram_ydl_key
    key = _extract_cache_key(opts)
    with _instagram_lock:
        _instagram_throttle()
        if _instagram_ydl is None or _instagram_ydl_key != key:
            if _instagram_ydl is not None:
                _instagram_ydl.close()
            _instagram_ydl = yt_dlp.YoutubeDL(opts)
            _instagram_ydl_key = key
        return _extract_info_with_retry(
            _instagram_ydl,
            url,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )


def _apply_network_opts(opts: dict) -> None:
    cookies = get_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
        logger.debug("yt-dlp using cookies file %s", cookies)
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    if shutil.which("aria2c"):
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "8", "-s", "8", "-k", "1M", "--file-allocation=none"],
        }


def is_youtube_bot_check(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return (
        "sign in to confirm" in text
        or "confirm you're not a bot" in text
        or "confirm you are not a bot" in text
        or ("not a bot" in text and "youtube" in text)
    )


def youtube_bot_check_hint() -> str:
    cookies = get_cookies_file()
    if cookies:
        return (
            "YouTube blocked this server (bot check), even with cookies. "
            f"Refresh YouTube cookies in {cookies} (same VPS IP helps), or try again later."
        )
    return (
        "YouTube blocked this server (bot check). "
        "Optional: add YouTube cookies to data/cookies.txt. "
        "Spotify will also try SoundCloud and YouTube mirrors automatically."
    )


def _build_ydl_opts(
    *,
    audio_preferred: bool,
    output_dir: Path,
    progress_callback: ProgressCallback | None,
    max_height: int | None = None,
    url: str | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    height = max_height if max_height is not None else MAX_VIDEO_HEIGHT
    output_template = str(output_dir / "%(title).200B.%(ext)s")
    opts: dict = {
        "outtmpl": output_template,
        "format": _audio_format() if audio_preferred else _video_format(height, url),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 16 * 1024 * 1024,
        "skip_unavailable_fragments": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "extractor_args": {
            # Prefer clients that work without PO tokens / honor cookies.
            # Avoid ios-first (ignores cookies) and skip webpage (hurts bot checks).
            "youtube": {
                "player_client": [
                    "android_vr",
                    "tv",
                    "web_embedded",
                    "mweb",
                    "android",
                    "web",
                ],
            },
        },
        "progress_hooks": [_progress_hook(progress_callback, cancel_check)],
        "postprocessors": [],
    }

    _apply_network_opts(opts)
    _maybe_impersonate(opts, url)

    # Keep Instagram polite but don't add a full second between every request
    if url and _is_instagram_url(url):
        opts["sleep_interval_requests"] = 0
        opts["socket_timeout"] = 15

    if audio_preferred:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ]
        opts["postprocessor_args"] = {"ffmpeg": ["-threads", "2"]}

    return opts


def _video_format(height: int, url: str | None = None) -> str:
    # Instagram: prefer a single progressive MP4 under ~25 MB so Telegram can
    # fetch the CDN URL (instant) — avoid video+audio merge (forces VPS download).
    if url and _is_instagram_url(url):
        return (
            "best[ext=mp4][vcodec!=none][acodec!=none][filesize<=25M]/"
            "best[ext=mp4][vcodec!=none][acodec!=none][height<=720][filesize<=40M]/"
            f"best[ext=mp4][vcodec!=none][acodec!=none][height<={height}]/"
            "best[ext=mp4][vcodec!=none][acodec!=none]/"
            f"best[height<={height}]/"
            "best"
        )

    # YouTube/etc.: do NOT filter on filesize — unknown size excludes every format
    # and triggers "Requested format is not available". Compress after download.
    return (
        f"bv*[height<={height}]+ba/b[height<={height}]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/"
        "bv*+ba/b/"
        "bestvideo+bestaudio/"
        "best"
    )


def _relaxed_video_format(height: int, url: str | None = None) -> str:
    if url and _is_instagram_url(url):
        return "best[ext=mp4][vcodec!=none][acodec!=none]/best"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b/best"


def _format_fallback_chain(
    height: int,
    url: str | None,
    *,
    audio_preferred: bool = False,
) -> tuple[str, ...]:
    if audio_preferred:
        return (
            "bestaudio/best",
            "bestaudio[ext=m4a]/bestaudio",
            "best",
        )
    if url and _is_instagram_url(url):
        return (
            "best[ext=mp4][vcodec!=none][acodec!=none]/best",
            "best",
        )
    return (
        _relaxed_video_format(height, url),
        "bv*+ba/b",
        "bestvideo+bestaudio/best",
        "best",
        "worst",
    )


def _maybe_compress(
    path: Path,
    *,
    is_audio: bool,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
    source_url: str | None = None,  # noqa: ARG001 — kept for call-site compatibility
) -> Path:
    if is_audio:
        return path

    from bot.compressor import (
        compress_video,
        ffmpeg_available,
        light_compress_video,
        needs_compress,
        needs_light_compress,
    )

    if not ffmpeg_available():
        if needs_compress(path):
            logger.warning("File is large (%s) but ffmpeg is missing — cannot compress", path)
        return path

    try:
        # Light pass first: shrink typical reels toward ~half size (fast)
        if needs_light_compress(path):
            path = light_compress_video(
                path,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        # Hard pass if still over soft Telegram-friendly target
        if needs_compress(path):
            path = compress_video(
                path,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
    except DownloadCancelledError:
        raise
    except Exception as exc:
        logger.warning("Compression failed for %s: %s", path, exc)
        return path

    return path


def _is_format_unavailable(exc: DownloadError) -> bool:
    return "requested format is not available" in str(exc).lower()


def _audio_format() -> str:
    # Avoid filesize filters — unknown size makes every format "unavailable"
    return "bestaudio/best/bestaudio[ext=m4a]/bestaudio[ext=mp3]"


def _is_audio_url(url: str) -> bool:
    # Spotify is handled separately (DRM) — not listed here for yt-dlp audio path
    audio_hosts = (
        "soundcloud.com",
        "music.apple.com",
        "deezer.com",
        "bandcamp.com",
    )
    lower = url.lower()
    if url.startswith("ytsearch"):
        return False
    return any(host in lower for host in audio_hosts)


_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}


def _codec_is_none(value: object) -> bool:
    if value is None:
        return True
    return str(value).lower() in {"none", "null", ""}


def _info_has_video_stream(info: dict) -> bool:
    """True if info or its formats include a real video codec."""
    vcodec = info.get("vcodec")
    if vcodec is not None and not _codec_is_none(vcodec):
        return True
    for group in (info.get("requested_formats"), info.get("formats")):
        if not group:
            continue
        for fmt in group:
            vc = fmt.get("vcodec")
            if vc is not None and not _codec_is_none(vc):
                return True
    height = info.get("height")
    width = info.get("width")
    if height or width:
        return True
    ext = (info.get("ext") or "").lower()
    if ext in {"mp4", "webm", "mkv", "mov", "m4v"}:
        return True
    return False


def _result_is_audio(
    info: dict | None,
    *,
    url: str,
    audio_preferred: bool,
    path: Path | None = None,
) -> bool:
    """Decide audio vs video. Missing vcodec must NOT mean audio (Instagram bug)."""
    if audio_preferred:
        return True

    # Reels / shorts platforms are always video unless force_audio
    if _is_instagram_url(url):
        return False
    lower = url.lower()
    if any(
        host in lower
        for host in (
            "tiktok.com",
            "youtube.com/watch",
            "youtu.be/",
            "youtube.com/shorts",
            "facebook.com",
            "fb.watch",
            "twitter.com",
            "x.com/",
        )
    ):
        if path and path.suffix.lower() in _AUDIO_EXTS and not _info_has_video_stream(info or {}):
            return True
        return False

    if path is not None:
        suffix = path.suffix.lower()
        if suffix in _VIDEO_EXTS:
            return False
        if suffix in _AUDIO_EXTS:
            return True

    if not info:
        return False

    if _info_has_video_stream(info):
        return False

    vcodec = info.get("vcodec")
    # Explicit audio-only stream
    if vcodec is not None and _codec_is_none(vcodec):
        acodec = info.get("acodec")
        if acodec is not None and not _codec_is_none(acodec):
            return True
        ext = (info.get("ext") or "").lower()
        if ext in {"mp3", "m4a", "opus", "ogg", "wav", "flac", "aac"}:
            return True

    # Missing vcodec → default video (yt-dlp often omits it on the top-level dict)
    return False


def _progress_hook(callback: ProgressCallback | None, cancel_check: CancelCheck | None = None):
    last_status = {"text": "", "at": 0.0}

    def hook(d: dict) -> None:
        if cancel_check:
            cancel_check()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0

            if total and total > get_download_size_cap():
                raise DownloadError(
                    f"File is {_format_size(total)} — exceeds {_format_size(get_download_size_cap())} download cap"
                )
            if downloaded > get_download_size_cap():
                raise DownloadError(
                    f"Download exceeded {_format_size(get_download_size_cap())} — stopped early"
                )

            if not callback:
                return

            from bot.messages import download_progress

            text = download_progress(downloaded, total or None, max_bytes=get_max_file_size())
        elif status == "finished":
            if not callback:
                return
            text = "📤 <b>Uploading…</b>"
        else:
            return

        now = time.monotonic()
        if text == last_status["text"]:
            return
        if status == "downloading" and now - last_status["at"] < 1.5:
            return

        last_status["text"] = text
        last_status["at"] = now
        callback(text)

    return hook


def _sleep_with_cancel(seconds: float, cancel_check: CancelCheck | None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_check:
            cancel_check()
        time.sleep(min(0.25, deadline - time.monotonic()))


def _direct_url_from_info(info: dict, *, prefer_small: bool = False) -> str | None:
    """Pick a progressive (A+V) HTTP URL Telegram can hotlink.

    When prefer_small (Instagram), favor ~25 MB progressive MP4 so Telegram
    can fetch it without the VPS downloading/uploading.
    """
    multi = bool(info.get("requested_formats") and len(info["requested_formats"]) > 1)
    if not multi:
        direct_url = info.get("url")
        if _is_usable_direct_url(info, direct_url):
            file_size = _estimate_size(info)
            if file_size and file_size > get_max_file_size():
                return None
            # Prefer scanning formats for a smaller progressive when speed matters
            if not (prefer_small and file_size and file_size > 25 * 1024 * 1024):
                return direct_url

    candidates: list[tuple[int, int, str]] = []  # (size_or_inf, height, url)
    for fmt in info.get("formats") or []:
        url = fmt.get("url")
        if not _is_usable_direct_url(fmt, url):
            continue
        if fmt.get("acodec") in (None, "none") or fmt.get("vcodec") in (None, "none"):
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if size and size > get_max_file_size():
            continue
        height = int(fmt.get("height") or 0)
        size_key = int(size) if size else 10**15
        candidates.append((size_key, height, url))

    if not candidates:
        return None

    if prefer_small:
        under_25 = [c for c in candidates if c[0] <= 25 * 1024 * 1024]
        pool = under_25 or [c for c in candidates if c[0] <= STANDARD_UPLOAD_LIMIT] or candidates
        # Best quality among the small pool
        best = max(pool, key=lambda c: (c[1], -c[0]))
        return best[2]

    best = max(candidates, key=lambda c: (c[1], -c[0]))
    return best[2]


def _is_usable_direct_url(meta: dict, url: str | None) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    protocol = (meta.get("protocol") or "").lower()
    if any(p in protocol for p in ("m3u8", "dash", "ism")):
        return False
    ext = (meta.get("ext") or "").lower()
    if ext in {"m3u8", "mpd", "f4m"}:
        return False
    return True


def _find_downloaded_file(directory: Path) -> Path | None:
    files = [
        f
        for f in directory.iterdir()
        if f.is_file() and not f.name.endswith((".part", ".ytdl", ".temp"))
    ]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_size)


def cleanup_file(path: Path) -> None:
    """Remove downloaded file and its parent job directory."""
    _cleanup_job_dir(path.parent if path.is_file() else path)


def _cleanup_job_dir(directory: Path) -> None:
    try:
        if not directory.exists() or directory == DOWNLOAD_DIR:
            return
        for item in directory.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
            elif item.is_dir():
                _cleanup_job_dir(item)
                item.rmdir()
        directory.rmdir()
    except OSError as exc:
        logger.warning("Cleanup failed for %s: %s", directory, exc)
