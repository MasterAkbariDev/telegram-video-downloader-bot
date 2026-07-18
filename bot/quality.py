"""Quality-picker helpers for long videos (YouTube / X / adult)."""

from __future__ import annotations

from urllib.parse import urlparse

_ADULT_HOSTS = (
    "pornhub.com",
    "xvideos.com",
    "xhamster.com",
    "redtube.com",
    "xnxx.com",
    "spankbang.com",
    "eporner.com",
    "youporn.com",
    "tube8.com",
    "beeg.com",
    "beeg.site",
    "beeg.team",
)

QUALITY_LADDER = (360, 480, 720, 1080)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def is_youtube_shorts(url: str) -> bool:
    return "youtube.com/shorts/" in (url or "").lower()


def is_adult_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _ADULT_HOSTS)


def is_youtube_long_url(url: str) -> bool:
    """YouTube watch / share / music — not Shorts."""
    if is_youtube_shorts(url):
        return False
    lower = (url or "").lower()
    return any(
        part in lower
        for part in (
            "youtube.com/watch",
            "youtu.be/",
            "m.youtube.com/watch",
            "music.youtube.com/",
            "youtube.com/live/",
            "youtube.com/embed/",
        )
    )


def is_x_video_host(url: str) -> bool:
    host = _host(url)
    return host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(
        ".twitter.com"
    )


def needs_quality_picker(url: str) -> bool:
    """True when this link type should offer a height picker (if 2+ heights exist)."""
    if not url:
        return False
    lower = url.lower()
    if is_youtube_shorts(url):
        return False
    if any(
        h in lower
        for h in (
            "instagram.com",
            "instagr.am",
            "pinterest.com",
            "pin.it",
            "tiktok.com",
            "spotify.com",
            "soundcloud.com",
        )
    ):
        return False
    if is_youtube_long_url(url):
        return True
    if is_x_video_host(url):
        return True
    if is_adult_url(url):
        return True
    return False
