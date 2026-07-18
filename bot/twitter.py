"""X (Twitter) photo / video extraction via syndication CDN APIs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import httpx

from bot.config import YTDLP_PROXY
from bot.fast_download import DESKTOP_UA

try:
    from bot.jobs import CancelCheck
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_STATUS_RE = re.compile(
    r"(?:twitter\.com|x\.com)/(?:[^/]+/)?status(?:es)?/(\d+)",
    re.IGNORECASE,
)
_STATUS_BARE_RE = re.compile(r"(?:twitter\.com|x\.com)/i/status/(\d+)", re.IGNORECASE)


@dataclass
class XSlide:
    kind: str  # "image" | "video"
    url: str


def is_x_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(
        ".twitter.com"
    )


def status_id(url: str) -> str | None:
    for pattern in (_STATUS_RE, _STATUS_BARE_RE):
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def resolve_x_post(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """
    Resolve an X/Twitter status to photos (CDN) or a video URL.

    Photos/albums are returned as CDN hotlinks (Telegram fetches them).
    Videos prefer a progressive MP4 CDN URL.
    """
    from bot.downloader import AlbumItem, MediaResult

    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("🐦 <b>Fetching X post…</b>")

    tid = status_id(url)
    if not tid:
        return None

    data = _fetch_tweet(tid)
    if not data:
        return None

    uploader = _uploader_from_tweet(data)
    title = _title_from_tweet(data) or "X post"
    slides = _slides_from_tweet(data)
    if not slides:
        return None

    images = [s for s in slides if s.kind == "image"]
    videos = [s for s in slides if s.kind == "video"]

    # Video post — send best MP4 CDN URL
    if videos:
        if progress_callback:
            progress_callback("📤 <b>Sending X video…</b>")
        return MediaResult(
            title=title,
            is_audio=False,
            file_size=None,
            direct_url=videos[0].url,
            used_direct=True,
            uploader=uploader,
        )

    if not images:
        return None

    if progress_callback:
        progress_callback("📤 <b>Sending X photos…</b>")
    if len(images) == 1:
        return MediaResult(
            title=title,
            is_audio=False,
            file_size=None,
            direct_url=images[0].url,
            used_direct=True,
            is_image=True,
            uploader=uploader,
        )

    album = [
        AlbumItem(kind="image", url=s.url, path=None, file_size=None) for s in images[:10]
    ]
    logger.info("X album %s: %d CDN photo(s)", tid, len(album))
    return MediaResult(
        title=title,
        is_audio=False,
        file_size=None,
        used_direct=True,
        is_image=True,
        album=album,
        uploader=uploader,
    )


def _fetch_tweet(tid: str) -> dict | None:
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    urls = [
        f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=en&token=0",
        f"https://api.vxtwitter.com/Twitter/status/{tid}",
        f"https://api.fxtwitter.com/status/{tid}",
    ]
    with httpx.Client(
        headers=headers,
        proxy=YTDLP_PROXY,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        for api_url in urls:
            try:
                resp = client.get(api_url)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                if not isinstance(data, dict):
                    continue
                if "tweet" in data and isinstance(data["tweet"], dict):
                    data = data["tweet"]
                if (
                    data.get("photos")
                    or data.get("mediaDetails")
                    or data.get("mediaURLs")
                    or data.get("media")
                    or data.get("video")
                    or data.get("media_extended")
                ):
                    return data
            except Exception as exc:
                logger.debug("X API fetch failed %s: %s", api_url, exc)
    return None


def _slides_from_tweet(data: dict) -> list[XSlide]:
    slides: list[XSlide] = []

    for photo in data.get("photos") or []:
        url = photo.get("url") or photo.get("media_url_https")
        if url:
            slides.append(XSlide(kind="image", url=_orig_photo_url(url)))

    video = data.get("video")
    if isinstance(video, dict):
        best = _best_mp4_from_variants(video.get("variants") or [])
        if best:
            slides.append(XSlide(kind="video", url=best))

    for media in data.get("mediaDetails") or []:
        mtype = (media.get("type") or "").lower()
        if mtype == "photo":
            url = media.get("media_url_https") or media.get("media_url")
            if url and not _has_image(slides, url):
                slides.append(XSlide(kind="image", url=_orig_photo_url(url)))
        elif mtype in {"video", "animated_gif"}:
            vi = media.get("video_info") or {}
            best = _best_mp4_from_variants(vi.get("variants") or [])
            if best and not any(s.kind == "video" for s in slides):
                slides.append(XSlide(kind="video", url=best))

    for url in data.get("mediaURLs") or []:
        if not url:
            continue
        if "video.twimg.com" in url or str(url).endswith(".mp4"):
            if not any(s.kind == "video" for s in slides):
                slides.append(XSlide(kind="video", url=str(url)))
        elif "pbs.twimg.com" in url and not _has_image(slides, url):
            slides.append(XSlide(kind="image", url=_orig_photo_url(str(url))))

    for item in data.get("media_extended") or []:
        url = item.get("url")
        mtype = (item.get("type") or "").lower()
        if not url:
            continue
        if mtype in {"video", "gif"} or "video.twimg.com" in url:
            if not any(s.kind == "video" for s in slides):
                slides.append(XSlide(kind="video", url=str(url)))
        elif mtype == "image" or "pbs.twimg.com" in url:
            if not _has_image(slides, url):
                slides.append(XSlide(kind="image", url=_orig_photo_url(str(url))))

    media = data.get("media")
    if isinstance(media, dict):
        for photo in media.get("photos") or []:
            url = photo.get("url") or photo.get("media_url_https")
            if url and not _has_image(slides, url):
                slides.append(XSlide(kind="image", url=_orig_photo_url(str(url))))
        vid = media.get("video") or media.get("videos")
        if isinstance(vid, dict):
            best = vid.get("url") or _best_mp4_from_variants(vid.get("variants") or [])
            if best and not any(s.kind == "video" for s in slides):
                slides.append(XSlide(kind="video", url=str(best)))
        elif isinstance(vid, list):
            for v in vid:
                url = (v or {}).get("url")
                if url and not any(s.kind == "video" for s in slides):
                    slides.append(XSlide(kind="video", url=str(url)))
                    break

    return slides


def _has_image(slides: list[XSlide], url: str) -> bool:
    base = str(url).split("?", 1)[0]
    return any(s.kind == "image" and s.url.startswith(base) for s in slides)


def _best_mp4_from_variants(variants: list) -> str | None:
    best_url = None
    best_bitrate = -1
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        ctype = (variant.get("content_type") or variant.get("type") or "").lower()
        url = variant.get("url") or variant.get("src")
        if not url or "mp4" not in ctype:
            continue
        try:
            bitrate = int(variant.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        if bitrate >= best_bitrate:
            best_bitrate = bitrate
            best_url = str(url)
    return best_url


def _orig_photo_url(url: str) -> str:
    """Request original-size pbs.twimg.com image when possible."""
    if "pbs.twimg.com" not in url:
        return url
    if "name=" in url:
        return re.sub(r"name=[^&]+", "name=orig", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}name=orig"


def _uploader_from_tweet(data: dict) -> str | None:
    user = data.get("user") or data.get("author") or {}
    if isinstance(user, dict):
        return (
            user.get("screen_name")
            or user.get("screenName")
            or user.get("username")
            or user.get("name")
        )
    return data.get("user_name") or data.get("user_screen_name")


def _title_from_tweet(data: dict) -> str:
    text = data.get("text") or data.get("full_text") or data.get("tweetText") or ""
    text = re.sub(r"https?://t\.co/\w+", "", str(text)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > 100:
        text = text[:97].rstrip() + "…"
    return text
