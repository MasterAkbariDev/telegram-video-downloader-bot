"""HikerAPI (hikerapi.com) — paid, managed Instagram data API.

Built by the instagrapi maintainers: it runs its own residential-proxy pool
and pool of authenticated accounts server-side, so *we* never have to run a
risky login from our own server IP. This is how commercial-scale Instagram
tools reach login-walled content reliably — maintaining that infrastructure
ourselves (one account, one server IP) doesn't scale and keeps hitting
Instagram's risk system, as our own auto-login has.

Used only as a last-resort fallback, after free extraction (yt-dlp +
anonymous HTML scraping) has already failed — never on the normal/public
post path, so the vast majority of downloads cost nothing.
"""

from __future__ import annotations

import logging
from typing import Callable

import httpx

from bot.config import HIKERAPI_KEY, YTDLP_PROXY

try:
    from bot.jobs import CancelCheck
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_MEDIA_BY_URL = "https://api.hikerapi.com/v1/media/by/url"


def hikerapi_configured() -> bool:
    return bool(HIKERAPI_KEY)


def resolve_via_hikerapi(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Resolve an Instagram post/reel/carousel via HikerAPI. Returns a
    MediaResult, or None if not configured / nothing usable came back."""
    from bot.downloader import AlbumItem, MediaResult

    if not HIKERAPI_KEY:
        return None
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("📸 <b>Trying managed API…</b>")

    try:
        with httpx.Client(proxy=YTDLP_PROXY, timeout=30.0, follow_redirects=True) as client:
            resp = client.get(
                _MEDIA_BY_URL,
                params={"url": url},
                headers={"x-access-key": HIKERAPI_KEY},
            )
        resp.raise_for_status()
        media = resp.json()
    except Exception as exc:
        logger.warning("HikerAPI request failed for %s: %s", url[:80], exc)
        return None

    if not isinstance(media, dict):
        return None

    uploader = ((media.get("user") or {}).get("username")) or None
    media_type = media.get("media_type")

    if media_type == 8:  # carousel
        resources = media.get("resources") or []
        album: list[AlbumItem] = []
        for item in resources:
            if not isinstance(item, dict):
                continue
            video_url = item.get("video_url")
            if video_url:
                album.append(AlbumItem(kind="video", url=video_url, path=None, file_size=None))
                continue
            thumb = item.get("thumbnail_url")
            if thumb:
                album.append(AlbumItem(kind="image", url=thumb, path=None, file_size=None))
        if not album:
            return None
        if len(album) == 1:
            item = album[0]
            return MediaResult(
                title="Instagram post",
                is_audio=False,
                file_size=None,
                direct_url=item.url,
                used_direct=True,
                is_image=item.kind == "image",
                uploader=uploader,
            )
        return MediaResult(
            title="Instagram post",
            is_audio=False,
            file_size=None,
            used_direct=True,
            is_image=all(a.kind == "image" for a in album),
            album=album,
            uploader=uploader,
        )

    if media_type == 2:  # video / reel
        video_url = media.get("video_url")
        if not video_url:
            return None
        return MediaResult(
            title="Instagram post",
            is_audio=False,
            file_size=None,
            direct_url=video_url,
            used_direct=True,
            is_image=False,
            uploader=uploader,
        )

    if media_type == 1:  # photo
        candidates = ((media.get("image_versions2") or {}).get("candidates")) or []
        best = _best_candidate(candidates) or media.get("thumbnail_url")
        if not best:
            return None
        return MediaResult(
            title="Instagram post",
            is_audio=False,
            file_size=None,
            direct_url=best,
            used_direct=True,
            is_image=True,
            uploader=uploader,
        )

    return None


def _best_candidate(candidates: list) -> str | None:
    best_url = None
    best_area = -1
    for c in candidates:
        if not isinstance(c, dict):
            continue
        candidate_url = c.get("url")
        if not candidate_url:
            continue
        try:
            area = int(c.get("width") or 0) * int(c.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        if area >= best_area:
            best_area = area
            best_url = candidate_url
    return best_url
