"""Pinterest pin extraction (images + videos via pinimg CDN)."""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

from bot.config import YTDLP_PROXY
from bot.fast_download import DESKTOP_UA

try:
    from bot.jobs import CancelCheck
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image(?::url|:secure_url)?[\"']",
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']'
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:title[\"']",
    re.IGNORECASE,
)
_ORIGINAL_IMG_RE = re.compile(
    r"https://i\.pinimg\.com/originals/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]+\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)
_VIDEO_RE = re.compile(
    r"https://v\d*\.pinimg\.com/videos/(?:mc/)?(?:\d+p|h264|expMp4)/[0-9a-f/]+[^\"'\s<>]+\.mp4",
    re.IGNORECASE,
)


def is_pinterest_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    if host == "pin.it" or host.endswith(".pin.it"):
        return True
    return host == "pinterest.com" or host.endswith(".pinterest.com") or host.startswith(
        "pinterest."
    )


def resolve_pinterest(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Resolve a Pinterest pin — Telegram fetches pinimg CDN URLs directly."""
    from bot.downloader import MediaResult

    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("📌 <b>Reading Pinterest pin…</b>")

    title, image_url, video_url = scrape_pinterest_pin(url)
    if not image_url and not video_url:
        return None

    display = title or "Pinterest pin"

    # Videos: pinimg CDN works with Telegram send_video (verified)
    if video_url:
        if progress_callback:
            progress_callback("📤 <b>Sending Pinterest video…</b>")
        return MediaResult(
            title=display,
            is_audio=False,
            file_size=None,
            direct_url=video_url,
            used_direct=True,
            uploader="Pinterest",
        )

    if image_url:
        if progress_callback:
            progress_callback("📤 <b>Sending Pinterest image…</b>")
        return MediaResult(
            title=display,
            is_audio=False,
            file_size=None,
            direct_url=image_url,
            used_direct=True,
            is_image=True,
            uploader="Pinterest",
        )

    return None


def scrape_pinterest_pin(url: str) -> tuple[str | None, str | None, str | None]:
    """Return (title, best_image_url, best_video_url)."""
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(
        headers=headers,
        proxy=YTDLP_PROXY,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        text = resp.text
        final_url = str(resp.url)

    title = _meta_content(_OG_TITLE_RE, text)
    if title:
        title = html_lib.unescape(title).strip()
        # Drop trailing Pinterest site suffix
        title = re.sub(r"\s*[|\-–]\s*Pinterest\s*$", "", title, flags=re.I).strip()

    video_url = _best_video(text)
    image_url = _best_image(text)

    if not image_url and not video_url:
        logger.info(
            "Pinterest scrape found no media for %s (final %s)",
            url[:80],
            final_url[:100],
        )

    return title or None, image_url, video_url


def _meta_content(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return unquote((match.group(1) or match.group(2) or "").strip()) or None


def _best_image(text: str) -> str | None:
    og = _meta_content(_OG_IMAGE_RE, text)
    if og and "pinimg.com" in og and "favicon" not in og.lower():
        # Prefer full-size originals when og:image is a sized CDN variant
        upgraded = re.sub(
            r"https://i\.pinimg\.com/(?:\d+x(?:\d+)?|736x|1200x)/",
            "https://i.pinimg.com/originals/",
            og,
            count=1,
            flags=re.I,
        )
        return upgraded

    originals = _ORIGINAL_IMG_RE.findall(text)
    if originals:
        return originals[0]
    return None


def _best_video(text: str) -> str | None:
    urls = list(dict.fromkeys(_VIDEO_RE.findall(text)))
    if not urls:
        return None

    def score(u: str) -> tuple[int, int]:
        lower = u.lower()
        # Prefer standard progressive 720p over expMp4 variants
        tier = 0
        if "/720p/" in lower:
            tier = 3
        elif "/h264/" in lower or re.search(r"/\d{3,4}p/", lower):
            tier = 2
        elif "expm4" in lower or "expmp4" in lower:
            tier = 1
        return (tier, len(u))

    urls.sort(key=score, reverse=True)
    return urls[0]
