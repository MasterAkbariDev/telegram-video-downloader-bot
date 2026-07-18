"""Backup media extraction when yt-dlp fails — lightweight HTTP scrapers."""

from __future__ import annotations

import html
import http.cookiejar
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

from bot.config import YTDLP_PROXY, get_cookies_file, get_max_file_size
from bot.fast_download import (
    DESKTOP_UA as _DESKTOP_UA,
    _parse_int_header,
    _total_size_from_headers,
    build_headers as _download_headers,
    download_http,
    head_size as _head_size,
)
from bot.messages import detect_platform

try:
    from bot.jobs import CancelCheck, DownloadCancelledError
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]
    DownloadCancelledError = RuntimeError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_OG_VIDEO_RE = re.compile(
    r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)

# Player-only patterns — main video, not sidebar/thumbnails
_MEDIA_DEFS_RE = re.compile(r'"mediaDefinitions"\s*:\s*(\[[\s\S]*?\])\s*[,}]')
_FLASHVARS_RE = re.compile(r"flashvars_[^=]+=\s*(\{[\s\S]*?\});")
_XVIDEO_URL_RE = re.compile(
    r"set(?:VideoUrl(?:High|Low)|VideoHLS)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_XHAMSTER_INITIALS_RE = re.compile(
    r"window\.initials\s*=\s*(\{[\s\S]*?\});\s*(?:</script>|window\.)",
    re.IGNORECASE,
)

_THUMB_HINTS = (
    "thumb",
    "thumbnail",
    "preview",
    "trailer",
    "teaser",
    "sprite",
    "poster",
    "avatar",
    "profilepic",
    "related",
    "/small/",
    "/tiny/",
    "/gif",
    "loading",
)

# Reject error pages / placeholders masquerading as video
MIN_VALID_BYTES = 50_000
BEEG_CDN = "https://video.beeg.com/"
PORNHUB_GET_MEDIA = "https://www.pornhub.com/video/get_media"

BOT_CHALLENGE_MSG = (
    "Site bot/age verification blocked automated access. "
    "Open the link in your browser, pass the check, then export cookies to "
    "data/cookies.txt (see COOKIES_FILE in .env)."
)


class BotChallengeError(RuntimeError):
    """Raised when a site shows captcha / age gate / countdown blocker."""


@dataclass
class _Extracted:
    title: str
    media_url: str
    is_audio: bool = False
    file_size: int | None = None
    referer: str | None = None


@dataclass
class _Page:
    url: str
    text: str


def fallback_resolve(
    url: str,
    output_dir: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """Try built-in extractors and download when yt-dlp fails."""
    from bot.downloader import FileTooLargeError, MediaResult

    with _client() as client:
        extracted = _extract(url, client)
        if _is_bad_media_url(extracted.media_url):
            raise RuntimeError("Backup downloader found a stream URL it cannot handle.")

        referer = extracted.referer or url
        file_size = extracted.file_size or _head_size(
            client, extracted.media_url, _download_headers(referer)
        )
        if file_size and file_size > get_max_file_size():
            raise FileTooLargeError(file_size)

        if not _prefer_download_to_disk(url):
            logger.info("Fallback direct URL for %s", url)
            return MediaResult(
                title=extracted.title,
                is_audio=extracted.is_audio,
                file_size=file_size,
                direct_url=extracted.media_url,
                used_direct=True,
            )

        if progress_callback:
            progress_callback("⬇️ <b>Backup download…</b>")

        ext = _guess_ext(extracted.media_url, extracted.is_audio)
        dest = output_dir / f"media.{ext}"
        download_http(
            client,
            extracted.media_url,
            dest,
            referer=referer,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        size = dest.stat().st_size
        if size < MIN_VALID_BYTES:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"Downloaded file is too small ({size} bytes) — not a valid video."
            )
        if size > get_max_file_size():
            dest.unlink(missing_ok=True)
            raise FileTooLargeError(size)

        return MediaResult(
            title=extracted.title,
            is_audio=extracted.is_audio,
            file_size=size,
            file_path=dest,
            used_direct=False,
        )


def _prefer_download_to_disk(url: str) -> bool:
    """Sites whose CDN URLs usually fail Telegram hotlinking — download first."""
    lower = url.lower()
    return any(
        host in lower
        for host in (
            # Instagram: try direct CDN first via yt-dlp path; fallback still prefers download
            # only for adult CDNs / social that block Telegram fetch
            "tiktok.com",
            "twitter.com",
            "x.com",
            "facebook.com",
            "fb.watch",
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
        )
    )


def _client() -> httpx.Client:
    cookies = _load_cookies()
    return httpx.Client(
        headers={"User-Agent": _DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
        cookies=cookies,
        proxy=YTDLP_PROXY,
        timeout=30.0,
        follow_redirects=True,
    )


def _load_cookies() -> httpx.Cookies:
    path = get_cookies_file()
    cookies = httpx.Cookies()
    if path:
        jar = http.cookiejar.MozillaCookieJar(path)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        except Exception as exc:
            logger.warning("Could not load cookies for fallback: %s", exc)
    return cookies


def _apply_site_cookies(cookies: httpx.Cookies, url: str) -> None:
    """Best-effort cookies that skip age gates (real browser cookies still work best)."""
    lower = url.lower()
    if "pornhub.com" in lower:
        for name, value in (
            ("accessAgeDisclaimerPH", "1"),
            ("cookieConsent", "1"),
            ("platform", "pc"),
            ("age_verified", "1"),
        ):
            cookies.set(name, value, domain=".pornhub.com", path="/")
    elif any(h in lower for h in ("redtube.com", "youporn.com", "tube8.com")):
        cookies.set("cookieConsent", "1", domain=".mindgeek.com", path="/")
        cookies.set("platform", "pc", domain=".mindgeek.com", path="/")


def _fetch_page(
    url: str,
    client: httpx.Client,
    *,
    mobile: bool = False,
    extra_headers: dict | None = None,
) -> _Page | None:
    try:
        ua = _MOBILE_UA if mobile else _DESKTOP_UA
        cookies = httpx.Cookies()
        for c in client.cookies.jar:
            cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        _apply_site_cookies(cookies, url)
        headers = {"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"}
        if extra_headers:
            headers.update(extra_headers)
        resp = client.get(url, headers=headers, cookies=cookies)
        if resp.status_code >= 400:
            return None
        return _Page(url=url, text=resp.text)
    except httpx.HTTPError as exc:
        logger.debug("Page fetch failed %s: %s", url, exc)
        return None


def _extract(url: str, client: httpx.Client) -> _Extracted:
    lower = url.lower()

    if "instagram.com" in lower or "instagr.am" in lower:
        result = _extract_instagram(url, client)
        if result:
            return result
    if "tiktok.com" in lower:
        result = _extract_tiktok(url, client)
        if result:
            return result
    if "twitter.com" in lower or "x.com" in lower:
        result = _extract_twitter(url, client)
        if result:
            return result
    if "facebook.com" in lower or "fb.watch" in lower:
        result = _extract_facebook(url, client)
        if result:
            return result

    # Adult sites — player JSON only (not page-wide CDN scan)
    if "pornhub.com" in lower:
        result = _extract_pornhub(url, client)
        if result:
            return result
    if "redtube.com" in lower or "youporn.com" in lower or "tube8.com" in lower:
        result = _extract_mindgeek(url, client)
        if result:
            return result
    if "xvideos.com" in lower or "xnxx.com" in lower:
        result = _extract_xvideos(url, client)
        if result:
            return result
    if "xhamster.com" in lower:
        result = _extract_xhamster(url, client)
        if result:
            return result
    if "spankbang.com" in lower:
        result = _extract_spankbang(url, client)
        if result:
            return result
    if "eporner.com" in lower:
        result = _extract_eporner(url, client)
        if result:
            return result
    if "beeg.com" in lower or "beeg.site" in lower:
        result = _extract_beeg(url, client)
        if result:
            return result

    page = _fetch_page(url, client)
    if page and _page_looks_like_video(page.text):
        candidates: list[str] = []
        found = _find_main_video_url(page.text)
        if found:
            candidates.append(found)
        title = _find_title(page.text)
        name, _ = detect_platform(page.url)
        if not title:
            title = f"{name} video"
        result = _finalize_extraction(client, title, candidates, referer=page.url)
        if result:
            return result

    raise RuntimeError("Backup downloader could not find media in this link.")


def _extract_instagram(url: str, client: httpx.Client) -> _Extracted | None:
    shortcode = _instagram_shortcode(url)
    candidates = [url]
    if shortcode:
        candidates.extend(
            [
                f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
                f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
            ]
        )
    for page_url in candidates:
        page = _fetch_page(page_url, client, mobile=True)
        if not page:
            continue
        media_url = _find_social_video_url(page.text)
        if media_url:
            title = _find_title(page.text) or f"Instagram {shortcode or 'video'}"
            return _Extracted(title=title, media_url=media_url, referer=page.url)
    return None


def _extract_tiktok(url: str, client: httpx.Client) -> _Extracted | None:
    # Prefer curl_cffi impersonation — plain httpx often gets 403 from TikTok.
    page_text, final_url = _fetch_tiktok_page(url, client)
    if not page_text:
        return None
    media_url = _find_social_video_url(page_text)
    if not media_url:
        return None
    return _Extracted(
        title=_find_title(page_text) or "TikTok video",
        media_url=media_url,
        referer=final_url or url,
    )


def _fetch_tiktok_page(url: str, client: httpx.Client) -> tuple[str | None, str | None]:
    try:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(
            url,
            impersonate="chrome",
            allow_redirects=True,
            timeout=30,
            proxy=YTDLP_PROXY,
        )
        if resp.status_code < 400 and resp.text:
            return resp.text, str(resp.url)
    except Exception as exc:
        logger.debug("TikTok curl_cffi fetch failed: %s", exc)

    page = _fetch_page(url, client, mobile=True)
    if not page:
        return None, None
    return page.text, page.url


def _extract_twitter(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page:
        return None
    media_url = _find_social_video_url(page.text)
    if not media_url or "video.twimg.com" not in media_url:
        return None
    return _Extracted(title=_find_title(page.text) or "X video", media_url=media_url, referer=page.url)


def _extract_facebook(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page:
        return None
    media_url = _find_main_video_url(page.text)
    if not media_url:
        return None
    return _Extracted(title=_find_title(page.text) or "Facebook video", media_url=media_url, referer=page.url)


def _extract_pornhub(url: str, client: httpx.Client) -> _Extracted | None:
    viewkey = _pornhub_viewkey(url)
    referer = url
    saw_challenge = False

    # API often works even when the HTML page is gated
    if viewkey:
        api_candidates = _pornhub_get_media_urls(client, viewkey, referer=referer)
        result = _finalize_extraction(client, "Video", api_candidates, referer=referer)
        if result:
            result.title = _find_title_from_pages(client, url, viewkey) or result.title
            return result

    page_urls = [url]
    if viewkey:
        page_urls.extend(
            [
                f"https://www.pornhub.com/view_video.php?viewkey={viewkey}",
                f"https://www.pornhub.com/embed/{viewkey}",
            ]
        )

    for page_url in page_urls:
        for mobile in (False, True):
            page = _fetch_page(page_url, client, mobile=mobile)
            if not page:
                continue
            if _is_bot_challenge_page(page.text):
                saw_challenge = True
                logger.info("PornHub bot/age challenge on %s", page_url)
                continue
            if not _page_looks_like_video(page.text):
                continue

            title = _find_title(page.text) or "Video"
            candidates: list[str] = []
            if viewkey:
                candidates.extend(_pornhub_get_media_urls(client, viewkey, referer=page.url))
            candidates.extend(_all_mindgeek_player_urls(page.text))
            result = _finalize_extraction(client, title, candidates, referer=page.url)
            if result:
                return result

    if saw_challenge:
        raise BotChallengeError(BOT_CHALLENGE_MSG)
    return None


def _find_title_from_pages(client: httpx.Client, url: str, viewkey: str) -> str | None:
    for page_url in (url, f"https://www.pornhub.com/embed/{viewkey}"):
        page = _fetch_page(page_url, client)
        if page and not _is_bot_challenge_page(page.text):
            title = _find_title(page.text)
            if title:
                return title
    return None


def _extract_beeg(url: str, client: httpx.Client) -> _Extracted | None:
    match = re.search(r"beeg\.(?:com|site|team)/-?(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    video_id = int(match.group(1))
    try:
        resp = client.get(
            f"https://store.externulls.com/facts/file/{video_id}",
            headers={"Referer": url},
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None

    file_obj = data.get("file") or {}
    duration = int(file_obj.get("fl_duration") or 0)
    if duration <= 0:
        logger.debug("Beeg file %s has no duration — not a valid video", video_id)
        return None

    title = "Video"
    for item in file_obj.get("data") or []:
        if item.get("cd_column") == "sf_name" and item.get("cd_value"):
            title = str(item["cd_value"])

    candidates: list[str] = []
    fallback = file_obj.get("fallback")
    if isinstance(fallback, str) and fallback.startswith("key="):
        candidates.append(BEEG_CDN + fallback)

    resources = file_obj.get("resources") or {}
    if isinstance(resources, dict):
        for key, val in resources.items():
            if isinstance(val, str) and val.startswith("key="):
                candidates.append(BEEG_CDN + val)

    candidates = _sort_quality_urls(candidates)
    return _finalize_extraction(client, title, candidates, referer=url)


def _extract_mindgeek(url: str, client: httpx.Client) -> _Extracted | None:
    """PornHub network: RedTube, YouPorn, Tube8."""
    return _extract_pornhub(url, client)


def _extract_xvideos(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page or not _page_looks_like_video(page.text):
        return None
    candidates = _all_xvideos_player_urls(page.text)
    return _finalize_extraction(client, _find_title(page.text) or "Video", candidates, referer=page.url)


def _extract_xhamster(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page or not _page_looks_like_video(page.text):
        return None
    candidates: list[str] = []
    url_found = _find_xhamster_player_url(page.text)
    if url_found:
        candidates.append(url_found)
    return _finalize_extraction(client, _find_title(page.text) or "Video", candidates, referer=page.url)


def _extract_spankbang(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page or not _page_looks_like_video(page.text):
        return None
    candidates: list[str] = []
    found = _find_main_video_url(page.text)
    if found:
        candidates.append(found)
    return _finalize_extraction(client, _find_title(page.text) or "Video", candidates, referer=page.url)


def _extract_eporner(url: str, client: httpx.Client) -> _Extracted | None:
    page = _fetch_page(url, client)
    if not page:
        return None
    candidates: list[str] = []
    match = re.search(r"EP\.video\s*=\s*(\{[\s\S]*?\});", page.text)
    if match:
        try:
            data = json.loads(match.group(1))
            sources = data.get("sources") or data.get("src") or {}
            if isinstance(sources, dict):
                candidates.extend(
                    v for v in sources.values() if isinstance(v, str) and v.startswith("http")
                )
        except json.JSONDecodeError:
            pass
    found = _find_main_video_url(page.text)
    if found:
        candidates.append(found)
    return _finalize_extraction(client, _find_title(page.text) or "Video", candidates, referer=page.url)


def _find_main_video_url(text: str) -> str | None:
    """Extract the primary player video — never sidebar/thumbnail URLs."""
    for finder in (
        _find_og_video,
        _find_ld_json_video,
        _find_mindgeek_player_url,
        _find_xvideos_player_url,
        _find_xhamster_player_url,
    ):
        url = finder(text)
        if url:
            return url
    return None


def _find_social_video_url(text: str) -> str | None:
    """Social pages: og tags first, then first valid CDN in embed JSON."""
    url = _find_og_video(text) or _find_ld_json_video(text)
    if url:
        return url
    for match in re.finditer(
        r'"(?:video_url|playAddr|downloadAddr|contentUrl)":\s*"(https:[^"]+)"',
        text,
    ):
        cleaned = _clean_url(match.group(1))
        if cleaned and not _is_bad_media_url(cleaned) and not _is_likely_thumbnail(cleaned):
            return cleaned
    return None


def _find_og_video(text: str) -> str | None:
    match = _OG_VIDEO_RE.search(text)
    if not match:
        return None
    url = _clean_url(match.group(1))
    if url and not _is_bad_media_url(url) and not _is_likely_thumbnail(url):
        return url
    return None


def _find_mindgeek_player_url(text: str) -> str | None:
    urls = _all_mindgeek_player_urls(text)
    return urls[0] if urls else None


def _all_mindgeek_player_urls(text: str) -> list[str]:
    """PornHub / RedTube / YouPorn player config — all qualities, best first."""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_MEDIA_DEFS_RE, _FLASHVARS_RE):
        for match in pattern.finditer(text):
            blob = match.group(1)
            if pattern is _FLASHVARS_RE:
                urls = _all_from_flashvars(blob)
            else:
                try:
                    data = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                urls = _all_from_media_definitions(data)
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    found.append(url)
    return found


def _pornhub_viewkey(url: str) -> str | None:
    match = re.search(r"[?&]viewkey=([^&]+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/view_video\.php[^?]*[?&]viewkey=([^&]+)", url, re.IGNORECASE)
    return match.group(1) if match else None


def _pornhub_get_media_urls(client: httpx.Client, viewkey: str, *, referer: str) -> list[str]:
    cookies = httpx.Cookies()
    for c in client.cookies.jar:
        cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    _apply_site_cookies(cookies, referer)
    try:
        resp = client.get(
            PORNHUB_GET_MEDIA,
            params={"v": viewkey},
            headers={
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
            },
            cookies=cookies,
        )
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return _all_from_media_definitions(data)
    return []


def _finalize_extraction(
    client: httpx.Client,
    title: str,
    candidates: list[str],
    *,
    referer: str,
) -> _Extracted | None:
    picked = _first_valid_media_url(client, candidates, referer=referer)
    if not picked:
        return None
    media_url, file_size = picked
    return _Extracted(
        title=title,
        media_url=media_url,
        file_size=file_size,
        referer=referer,
    )


def _page_looks_like_video(text: str) -> bool:
    """Ensure the page is a real video, not a removed/empty listing."""
    if _is_bot_challenge_page(text):
        return False
    lower = text.lower()
    if any(
        phrase in lower
        for phrase in (
            "page not found",
            "video has been removed",
            "content unavailable",
            "this page is not available",
            "error 404",
            "error 410",
            "gone",
        )
    ):
        return False
    if _find_title(text) and (
        "video_duration" in lower
        or "mediaplayer" in lower
        or "flashvars_" in lower
        or '"mediadefinitions"' in lower
        or "og:video" in lower
    ):
        return True
    return bool(_find_og_video(text) or _find_ld_json_video(text))


def _is_bot_challenge_page(text: str) -> bool:
    lower = text.lower()
    markers = (
        "age-verification",
        "ageverification",
        "age verification",
        "human verification",
        "verify you are human",
        "verify you're a human",
        "cf-browser-verification",
        "challenge-form",
        "g-recaptcha",
        "recaptcha",
        "click here to continue",
        "disabled_until",
        "countdown",
        "js_disabled",
        "robot check",
        "not a robot",
        "captcha",
        "before you proceed",
        "confirm your age",
        "enter the site",
    )
    return any(m in lower for m in markers)


def _first_valid_media_url(
    client: httpx.Client,
    candidates: list[str],
    *,
    referer: str,
) -> tuple[str, int | None] | None:
    seen: set[str] = set()
    for raw in candidates:
        url = _clean_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        if _is_bad_media_url(url) or _is_likely_thumbnail(url):
            continue
        probe = _probe_media_url(client, url, referer=referer)
        if probe.ok:
            logger.info("Validated media URL (%s bytes): %s", probe.size or "?", url[:80])
            return url, probe.size
        logger.debug("Rejected media URL (size=%s): %s", probe.size, url[:80])
    return None


def _probe_media_url(client: httpx.Client, url: str, *, referer: str):
    headers = {"Referer": referer, "Range": "bytes=0-16383"}
    try:
        resp = client.get(url, headers=headers)
        if resp.status_code >= 400:
            return _Probe(False, None, None)
        content_type = (resp.headers.get("content-type") or "").lower()
        if any(t in content_type for t in ("text/html", "application/json", "text/plain")):
            return _Probe(False, None, content_type)
        size = _total_size_from_headers(resp.headers) or _parse_int_header(
            resp.headers.get("content-length")
        )
        body = resp.content[:32]
        if body and not _looks_like_video_bytes(body):
            if not any(t in content_type for t in ("video/", "octet-stream")):
                return _Probe(False, size, content_type)
        if size is not None and size < MIN_VALID_BYTES:
            return _Probe(False, size, content_type)
        return _Probe(True, size, content_type)
    except httpx.HTTPError:
        return _Probe(False, None, None)


@dataclass
class _Probe:
    ok: bool
    size: int | None
    content_type: str | None


def _looks_like_video_bytes(data: bytes) -> bool:
    if len(data) < 4:
        return False
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True
    if data[:4] == b"\x1aE\xdf\xa3":
        return True
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return True
    # Some CDNs omit ftyp in first range — allow octet-stream if enough data promised
    return False


def _sort_quality_urls(urls: list[str]) -> list[str]:
    quality_order = ("1080", "720", "480", "360", "240", "high", "medium", "low")

    def rank(url: str) -> int:
        lower = url.lower()
        for index, q in enumerate(quality_order):
            if q in lower:
                return index
        return len(quality_order)

    return sorted(urls, key=rank)


def _find_xvideos_player_url(text: str) -> str | None:
    urls = _all_xvideos_player_urls(text)
    return urls[0] if urls else None


def _all_xvideos_player_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _XVIDEO_URL_RE.finditer(text):
        cleaned = _clean_url(match.group(1))
        if cleaned and not _is_bad_media_url(cleaned) and not _is_likely_thumbnail(cleaned):
            urls.append(cleaned)
    return _sort_quality_urls(urls)


def _find_xhamster_player_url(text: str) -> str | None:
    match = _XHAMSTER_INITIALS_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return _video_from_xhamster_initials(data)


def _video_from_flashvars(blob: str) -> str | None:
    urls = _all_from_flashvars(blob)
    return urls[0] if urls else None


def _all_from_flashvars(blob: str) -> list[str]:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    urls: list[str] = []
    defs = data.get("mediaDefinitions")
    if isinstance(defs, list):
        urls.extend(_all_from_media_definitions(defs))
    for key in ("video_url", "videoUrl", "link_url"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            cleaned = _clean_url(val)
            if cleaned and not _is_bad_media_url(cleaned):
                urls.append(cleaned)
    return urls


def _best_from_media_definitions(items: list) -> str | None:
    urls = _all_from_media_definitions(items)
    return urls[0] if urls else None


def _all_from_media_definitions(items: list) -> list[str]:
    """All MP4 URLs from player definitions, highest quality first."""
    ranked: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fmt = (item.get("format") or "").lower()
        if fmt in ("hls", "m3u8", "dash"):
            continue
        video_url = item.get("videoUrl") or item.get("video_url")
        if not isinstance(video_url, str):
            continue
        cleaned = _clean_url(video_url)
        if not cleaned or _is_bad_media_url(cleaned) or _is_likely_thumbnail(cleaned):
            continue
        height = int(item.get("height") or item.get("quality") or 0)
        ranked.append((height, cleaned))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in ranked]


def _video_from_xhamster_initials(data: dict) -> str | None:
    video = data.get("videoModel") or data.get("xplayerSettings") or data
    if isinstance(video, dict):
        sources = video.get("sources") or video.get("downloadUrls") or video.get("mp4File")
        if isinstance(sources, dict):
            urls = [v for v in sources.values() if isinstance(v, str) and v.startswith("http")]
            picked = _pick_best_url(urls)
            if picked:
                return picked
        if isinstance(sources, str) and sources.startswith("http"):
            return _clean_url(sources)
        for key in ("h264", "h265", "mp4", "videoUrl"):
            val = video.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return _clean_url(val)
            if isinstance(val, dict):
                urls = [v for v in val.values() if isinstance(v, str) and v.startswith("http")]
                picked = _pick_best_url(urls)
                if picked:
                    return picked
    return None


def _pick_best_url(urls: list[str]) -> str | None:
    cleaned: list[str] = []
    for raw in urls:
        url = _clean_url(raw)
        if url and not _is_bad_media_url(url) and not _is_likely_thumbnail(url):
            cleaned.append(url)
    if not cleaned:
        return None
    # Prefer URLs that mention higher quality
    quality_order = ("1080", "720", "480", "high", "medium", "low")
    for q in quality_order:
        for url in cleaned:
            if q in url.lower():
                return url
    return cleaned[0]


def _find_ld_json_video(text: str) -> str | None:
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        url = _video_from_ld(data)
        if url:
            return url
    return None


def _video_from_ld(data) -> str | None:
    if isinstance(data, list):
        for item in data:
            url = _video_from_ld(item)
            if url:
                return url
        return None
    if not isinstance(data, dict):
        return None
    if data.get("@type") in ("VideoObject", "AudioObject"):
        for key in ("contentUrl", "embedUrl", "url"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http") and not _is_bad_media_url(val):
                if not _is_likely_thumbnail(val):
                    return _clean_url(val)
    for value in data.values():
        if isinstance(value, (dict, list)):
            url = _video_from_ld(value)
            if url:
                return url
    return None


def _find_title(text: str) -> str | None:
    match = _OG_TITLE_RE.search(text)
    if match:
        return _clean_text(match.group(1))
    match = _TITLE_TAG_RE.search(text)
    if match:
        return _clean_text(match.group(1))
    return None


def _instagram_shortcode(url: str) -> str | None:
    match = re.search(r"instagram\.com/(?:reel|p|tv)/([^/?#]+)", url, re.IGNORECASE)
    return match.group(1) if match else None


def _is_likely_thumbnail(url: str) -> bool:
    lower = url.lower()
    return any(hint in lower for hint in _THUMB_HINTS)


def _clean_url(raw: str) -> str:
    url = html.unescape(raw).strip()
    url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002f", "/")
    url = url.replace("\\u002F", "/")
    url = unquote(url)
    if url.startswith("//"):
        url = "https:" + url
    return url


def _clean_text(raw: str) -> str:
    return html.unescape(raw).strip()


def _is_bad_media_url(url: str) -> bool:
    lower = url.lower()
    return any(token in lower for token in (".m3u8", ".mpd", "/manifest", "blob:"))


def _guess_ext(url: str, is_audio: bool) -> str:
    if is_audio:
        return "mp3"
    path = urlparse(url).path.lower()
    for ext in (".mp4", ".webm", ".mov", ".m4v"):
        if path.endswith(ext):
            return ext.lstrip(".")
    return "mp4"
