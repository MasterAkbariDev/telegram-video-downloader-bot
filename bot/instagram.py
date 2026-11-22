"""Resolve and download the original media from Instagram posts and Stories."""

from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from yt_dlp.extractor.instagram import InstagramIE, InstagramStoryIE
from yt_dlp.utils import DownloadError

from bot.config import DOWNLOAD_DIR, YTDLP_PROXY
from bot.cdn_media import download_slides as _download_slides, is_media_cdn
from bot.fast_download import DESKTOP_UA, download_http, make_client

try:
    from bot.jobs import CancelCheck, DownloadCancelledError
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]
    DownloadCancelledError = RuntimeError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Skip profile / tiny thumbs
_SKIP_IMAGE_HINTS = (
    "s150x150",
    "s320x320",
    "/s150x",
    "/s320x",
    "profile",
    "avatar",
    "favicon",
    "emoji",
    "null.jpg",
)


@dataclass
class IgSlide:
    kind: str  # "image" | "video"
    url: str


_POST_PATH = re.compile(
    r"^/(?:[A-Za-z0-9_.]+/)?(?P<kind>p|reels?|tv)/(?P<code>[A-Za-z0-9_-]+)"
    r"(?:/embed(?:/captioned)?)?/?$", re.IGNORECASE
)
_STORY_PATH = re.compile(r"^/stories/[A-Za-z0-9_.]+(?:/(?P<id>[0-9]+))?/?$", re.IGNORECASE)
_SHARE_PATH = re.compile(r"^/share/(?:(?:p|reel|reels)/)?[A-Za-z0-9_-]+/?$", re.IGNORECASE)


def is_instagram_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {
                "instagram.com", "www.instagram.com", "m.instagram.com",
                "instagr.am", "www.instagr.am", "l.instagram.com",
            }
            and parsed.username is None
            and parsed.port in {None, 80, 443}
        )
    except ValueError:
        return False


def normalize_instagram_url(url: str) -> str:
    if not is_instagram_url(url):
        return url
    parsed = urlsplit(url)
    if _STORY_PATH.fullmatch(parsed.path) or _SHARE_PATH.fullmatch(parsed.path):
        return urlunsplit(("https", "www.instagram.com", parsed.path, parsed.query, ""))
    match = _POST_PATH.fullmatch(parsed.path)
    if match:
        kind = match["kind"].lower().removesuffix("s")
        return urlunsplit(("https", "www.instagram.com", f"/{kind}/{match['code']}/", parsed.query, ""))
    return url


def instagram_identifier(url: str) -> str:
    path = urlsplit(url).path
    if _SHARE_PATH.fullmatch(path):
        return "share"
    story = _STORY_PATH.fullmatch(path)
    if story:
        return f"story-{story['id']}" if story["id"] else "story"
    post = _POST_PATH.fullmatch(path)
    return post["code"] if post else "unknown"


def resolve_instagram_share(url: str, *, cancel_check: CancelCheck | None = None) -> str:
    path = urlsplit(url).path
    if not _SHARE_PATH.fullmatch(path):
        if _POST_PATH.fullmatch(path) or _STORY_PATH.fullmatch(path):
            return url
        raise RuntimeError("Send a link to an Instagram post, Reel, or Story.")
    try:
        with httpx.Client(proxy=YTDLP_PROXY, timeout=15.0) as client:
            for _ in range(4):
                if cancel_check:
                    cancel_check()
                # The desktop web client may receive a JavaScript shell.
                # Follow the HTTP redirect served to a plain link request.
                with client.stream("GET", url) as response:
                    location = response.headers.get("location")
                    if not response.is_redirect or not location:
                        break
                    target = urljoin(url, location)
                    if not is_instagram_url(target) or urlsplit(target).scheme != "https":
                        break
                    url = normalize_instagram_url(target)
                    path = urlsplit(url).path
                    if _POST_PATH.fullmatch(path) or _STORY_PATH.fullmatch(path):
                        return url
                    if not _SHARE_PATH.fullmatch(path):
                        break
    except httpx.HTTPError:
        pass
    raise RuntimeError("Instagram could not resolve this shared link. Try the original post link.")


class _CaptureMedia:
    """Read the product yt-dlp receives before its video-only conversion."""

    def __init__(self):
        super().__init__()
        self.products = []

    def _extract_product(self, product_info, *args, **kwargs):
        self.products.append(product_info)
        return super()._extract_product(product_info, *args, **kwargs)


class _CapturePostIE(_CaptureMedia, InstagramIE):
    pass


class _CaptureStoryIE(_CaptureMedia, InstagramStoryIE):
    pass


def _capture_media(ydl, url: str) -> dict:
    story = _STORY_PATH.fullmatch(urlsplit(url).path)
    extractor = _CaptureStoryIE() if story else _CapturePostIE()
    ydl.add_info_extractor(extractor)
    try:
        ydl.extract_info(url, download=False, process=False, ie_key=extractor.ie_key())
    except DownloadError:
        if not extractor.products:
            raise

    products = extractor.products
    if story and story["id"] and not urlsplit(url).path.startswith("/stories/highlights/"):
        products = [
            product for product in products
            if str(product.get("pk") or product.get("id", "")).split("_", 1)[0] == story["id"]
        ]
    if not products:
        raise DownloadError("Instagram did not return the requested media.")

    slides = []
    for product in products:
        items = _slides_from_media_object(product)
        if not items or any(not _is_media_cdn(item.url) for item in items):
            return {
                "slides": [],
                "incomplete": bool(product.get("carousel_media") or
                                   product.get("edge_sidecar_to_children") or
                                   product.get("media_type") == 8 or len(products) > 1),
            }
        slides.extend(items)
    username = (products[0].get("user") or {}).get("username")
    return {"slides": slides, "uploader": username, "anonymous": not ydl.params.get("cookiefile")}


def _is_media_cdn(url: str) -> bool:
    return is_media_cdn(url, ("cdninstagram.com", "fbcdn.net"))


def _download_ig_image(
    url: str, dest: Path, *, referer: str, accept: str, client: httpx.Client | None = None,
) -> None:
    if client is not None:
        download_http(client, url, dest, referer=referer, extra_headers={"Accept": accept}, skip_probe=True)
    else:
        with make_client() as owned:
            _download_ig_image(url, dest, referer=referer, accept=accept, client=owned)


def resolve_instagram_post(
    url: str, *, progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    prefer_telegram_url: bool = False, initial_media: dict | None = None,
):
    from bot.downloader import (
        AlbumItem, FileTooLargeError, MediaResult, _build_ydl_opts, _instagram_extract,
        _instagram_failure, _log_instagram_failure, instagram_login_hint,
    )

    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True)
    finished = False
    started = time.monotonic()
    is_story = bool(_STORY_PATH.fullmatch(urlsplit(url).path))
    title = "Instagram Story" if is_story else "Instagram post"
    try:
        for attempt in (1, 2):
            opts = _build_ydl_opts(
                audio_preferred=False, output_dir=job_dir, url=url,
                progress_callback=progress_callback, cancel_check=cancel_check,
            )
            source = "yt-dlp-product"
            error = None
            try:
                if attempt == 1 and initial_media is not None:
                    info = initial_media
                else:
                    info = _instagram_extract(url, opts, cancel_check=cancel_check, extract_attempt=_capture_media)
            except DownloadError as exc:
                error = exc
                info = {"slides": []}
                category, _ = _instagram_failure(exc)
                if category == "rate_limit":
                    raise RuntimeError("Instagram is rate-limiting requests. Try again later.") from None

            if info.get("incomplete"):
                raise RuntimeError("Instagram did not return every item in this carousel or Story.")
            if not info["slides"] and not is_story:
                source = "public-html"
                _, info["slides"] = scrape_instagram_slides(url, cancel_check=cancel_check)

            if not info["slides"]:
                if error:
                    category, _ = _instagram_failure(error)
                    if category == "authentication":
                        raise RuntimeError(instagram_login_hint()) from None
                    raise RuntimeError("Instagram did not return accessible media for this link.") from None
                return None  # A video may only have a DASH manifest.
            slides = info["slides"]
            # An album of up to 10 items fits in one atomic Telegram request.
            if prefer_telegram_url and not is_story and info.get("anonymous") and len(slides) <= 10:
                def download_fallback(on_progress, check_cancel):
                    local = resolve_instagram_post(
                        url, progress_callback=on_progress, cancel_check=check_cancel,
                        initial_media=info,
                    )
                    if local is None:
                        raise RuntimeError("Instagram could not refresh the media for this link.")
                    return local

                logger.info(
                    "platform=instagram media=%s stage=resolved mode=telegram_url items=%d duration=%.2fs",
                    instagram_identifier(url), len(slides), time.monotonic() - started,
                )
                return MediaResult(
                    title=title, is_audio=False, file_size=None, used_direct=True,
                    direct_url=slides[0].url if len(slides) == 1 else None,
                    album=[AlbumItem(kind=slide.kind, url=slide.url) for slide in slides]
                    if len(slides) > 1 else None,
                    is_image=all(slide.kind == "image" for slide in slides),
                    uploader=info.get("uploader"), telegram_url_fallback=download_fallback,
                )
            try:
                album = _download_slides(
                    slides, job_dir, progress_callback, cancel_check,
                    provider="Instagram", domains=("cdninstagram.com", "fbcdn.net"),
                    referer="https://www.instagram.com/",
                )
            except (DownloadCancelledError, FileTooLargeError):
                raise
            except (httpx.HTTPError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                _log_instagram_failure(url, source, "download", exc, attempt=attempt)
                category, code = _instagram_failure(exc)
                if attempt == 1 and (code in {401, 403, 404, 410, 500, 502, 503, 504}
                                     or isinstance(exc, httpx.TransportError)):
                    _cleanup_dir(job_dir)
                    job_dir.mkdir()
                    if cancel_check:
                        cancel_check()
                    continue
                if category == "rate_limit":
                    raise RuntimeError("Instagram is rate-limiting requests. Try again later.") from None
                raise RuntimeError("Instagram media could not be downloaded and validated.") from None
            break

        logger.info(
            "platform=instagram media=%s provider=%s stage=validated items=%d duration=%.2fs",
            instagram_identifier(url), source, len(album), time.monotonic() - started,
        )
        single = album[0] if len(album) == 1 else None
        finished = True
        return MediaResult(
            title=title,
            is_audio=False, file_size=sum(item.file_size for item in album),
            file_path=single.path if single else None, used_direct=False,
            is_image=all(item.kind == "image" for item in album),
            album=None if single else album, uploader=info.get("uploader"),
        )
    finally:
        if not finished:
            _cleanup_dir(job_dir)


def scrape_instagram_slides(
    url: str, *, cancel_check: CancelCheck | None = None,
) -> tuple[str, list[IgSlide]]:
    pages = [url, urlunsplit(("https", "www.instagram.com", urlsplit(url).path + "embed/", "", ""))]
    with httpx.Client(proxy=YTDLP_PROXY, timeout=7.0) as client:
        for page in pages:
            if cancel_check:
                cancel_check()
            try:
                response = client.get(page, headers={"User-Agent": DESKTOP_UA})
                if response.status_code == 429:
                    break
                if response.status_code != 200:
                    continue
                slides = _slides_from_html(response.text)
                if slides and all(_is_media_cdn(slide.url) for slide in slides):
                    path = urlsplit(url).path
                    if re.match(r"^/(reel|tv)/", path) and not any(s.kind == "video" for s in slides):
                        continue
                    return _page_title(response.text), slides
            except httpx.HTTPError:
                continue
    return "", []


def _slides_from_html(text: str) -> list[IgSlide]:
    slides: list[IgSlide] = []

    # Modern GraphQL shortcode media blob
    for key in ("xdt_shortcode_media", "shortcode_media"):
        for blob in _extract_json_objects_after_key(text, key):
            slides = _slides_from_media_object(blob)
            if len(slides) >= 1:
                return _dedupe_slides(slides)

    # Carousel via edge_sidecar_to_children
    for block in _extract_json_objects_after_key(text, "edge_sidecar_to_children"):
        return _slides_from_sidecar_obj(block)

    # Newer carousel_media arrays (balanced brackets)
    for arr in _extract_json_arrays_after_key(text, "carousel_media"):
        if isinstance(arr, list) and arr:
            return _slides_from_carousel_media(arr)

    # GraphQL-ish nodes with display_url / image_versions2
    node_slides = _slides_from_display_video_pairs(text)
    if node_slides:
        return _dedupe_slides(node_slides)

    # Single og:image (photo post)
    og = _OG_IMAGE_RE.search(text)
    if og:
        img = _clean_url(og.group(1))
        if img and not _skip_image(img):
            # If there's also og:video, this is a video post — ignore images
            if re.search(r'property=["\']og:video', text, re.I):
                return []
            return [IgSlide(kind="image", url=img)]

    return []


def _slides_from_media_object(obj: dict) -> list[IgSlide]:
    """Parse shortcode_media / xdt_shortcode_media node."""
    if not isinstance(obj, dict):
        return []
    # Nested product of GraphQL wrappers
    for key in ("media", "data", "items"):
        nested = obj.get(key)
        if isinstance(nested, dict) and (
            nested.get("edge_sidecar_to_children")
            or nested.get("carousel_media")
            or nested.get("display_url")
            or nested.get("image_versions2")
        ):
            obj = nested
            break

    sidecar = obj.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict):
        return _slides_from_sidecar_obj(sidecar)

    carousel = obj.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        return _slides_from_carousel_media(carousel)
    if obj.get("media_type") == 8:
        return []

    slide = _slide_from_node(obj)
    return [slide] if slide else []


def _slides_from_sidecar_obj(data: dict) -> list[IgSlide]:
    slides: list[IgSlide] = []
    edges = data.get("edges") if isinstance(data, dict) else None
    if not isinstance(edges, list):
        return slides
    for edge in edges:
        node = (edge or {}).get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        slide = _slide_from_node(node)
        if slide:
            slides.append(slide)
    return slides if len(slides) == len(edges) else []


def _slides_from_sidecar_json(raw: str) -> list[IgSlide]:
    try:
        data = json.loads(_fix_json(raw))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return _slides_from_sidecar_obj(data)
    return []


def _extract_json_objects_after_key(text: str, key: str) -> list[dict]:
    """Find `"key": { ... }` objects with balanced braces."""
    out: list[dict] = []
    for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*\{{', text):
        start = match.end() - 1
        blob = _slice_balanced(text, start, "{", "}")
        if not blob:
            continue
        try:
            data = json.loads(_fix_json(blob))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _extract_json_arrays_after_key(text: str, key: str) -> list[list]:
    out: list[list] = []
    for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*\[', text):
        start = match.end() - 1
        blob = _slice_balanced(text, start, "[", "]")
        if not blob:
            continue
        try:
            data = json.loads(_fix_json(blob))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out.append(data)
    return out


def _slice_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    if start >= len(text) or text[start] != open_ch:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _slides_from_carousel_media(arr: list) -> list[IgSlide]:
    slides: list[IgSlide] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        # media_type: 1 image, 2 video
        media_type = item.get("media_type")
        is_video = bool(
            media_type == 2
            or item.get("is_video")
            or item.get("video_versions")
            or item.get("video_url")
        )
        if is_video:
            url = None
            video_versions = item.get("video_versions") or []
            if isinstance(video_versions, list) and video_versions:
                url = (video_versions[0] or {}).get("url")
            if not url:
                url = item.get("video_url")
            if url:
                slides.append(IgSlide(kind="video", url=_clean_url(str(url))))
            # Never fall back to poster image for video slides
            continue

        candidates = (
            ((item.get("image_versions2") or {}).get("candidates"))
            if isinstance(item.get("image_versions2"), dict)
            else None
        )
        if isinstance(candidates, list) and candidates:
            url = _best_image_candidate_url(candidates)
            if url and not _skip_image(url):
                slides.append(IgSlide(kind="image", url=url))
                continue
        # display_resources: [{src, config_width, config_height}, ...]
        resources = item.get("display_resources")
        if isinstance(resources, list) and resources:
            url = _best_display_resource_url(resources)
            if url:
                slides.append(IgSlide(kind="image", url=url))
                continue
        display = item.get("display_url") or item.get("display_src")
        if display and not _skip_image(str(display)):
            slides.append(IgSlide(kind="image", url=_clean_url(str(display))))
    return slides if len(slides) == len(arr) else []


def _best_display_resource_url(resources: list) -> str | None:
    scored: list[tuple[int, str]] = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        url = r.get("src") or r.get("url")
        if not url or _skip_image(str(url)):
            continue
        try:
            w = int(r.get("config_width") or r.get("width") or 0)
            h = int(r.get("config_height") or r.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        scored.append((w * h, _clean_url(str(url))))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _best_image_candidate_url(candidates: list) -> str | None:
    """Pick the highest-resolution image URL (avoid cropped / tiny thumbs)."""
    scored: list[tuple[int, str]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        url = c.get("url")
        if not url or _skip_image(str(url)):
            continue
        try:
            w = int(c.get("width") or 0)
            h = int(c.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        scored.append((w * h, _clean_url(str(url))))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _slides_from_display_video_pairs(text: str) -> list[IgSlide]:
    """
    Fallback: collect full-res image candidates / display_urls.

    Instagram pages often embed unrelated video_url blobs (suggested reels).
    Only bail out when this looks like a *single* video post (1 display + video),
    not when we have multiple carousel images.
    """
    candidate_blocks = list(
        re.finditer(
            r'"image_versions2"\s*:\s*\{\s*"candidates"\s*:\s*(\[[\s\S]*?\])\s*\}',
            text,
        )
    )
    slides: list[IgSlide] = []
    seen: set[str] = set()
    if candidate_blocks:
        for match in candidate_blocks:
            # Prefer balanced array parse when possible
            arr = None
            try:
                arr = json.loads(_fix_json(match.group(1)))
            except json.JSONDecodeError:
                start = match.start(1)
                blob = _slice_balanced(text, start, "[", "]")
                if blob:
                    try:
                        arr = json.loads(_fix_json(blob))
                    except json.JSONDecodeError:
                        arr = None
            if not isinstance(arr, list):
                continue
            url = _best_image_candidate_url(arr)
            if not url:
                continue
            key = url.split("?", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            slides.append(IgSlide(kind="image", url=url))

    displays = [
        _clean_url(u)
        for u in re.findall(r'"display_url"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    ]
    for url in displays:
        if not url or _skip_image(url):
            continue
        key = url.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        slides.append(IgSlide(kind="image", url=_prefer_uncropped_url(url)))

    if not slides:
        return []

    has_video = bool(
        re.search(r'"video_url"\s*:\s*"', text)
        or re.search(r'property=["\']og:video', text, re.I)
    )
    # Single poster + video markers → reel/video; leave to yt-dlp
    if has_video and len(slides) <= 1:
        return []
    return slides


def _prefer_uncropped_url(url: str) -> str:
    """Best-effort: strip Instagram crop/resize hints when a fuller URL is encoded."""
    lower = url.lower()
    if any(x in lower for x in ("/s640x640", "/s480x480", "/s320x320", "c0.0.0.0")):
        pass
    return url


def _slide_from_node(node: dict) -> IgSlide | None:
    is_video = (
        node.get("media_type") == 2 or node.get("is_video") or
        node.get("__typename") == "GraphVideo" or node.get("video_versions") or node.get("video_url")
    )
    if is_video:
        vurl = node.get("video_url")
        if vurl:
            return IgSlide(kind="video", url=_clean_url(str(vurl)))
        versions = node.get("video_versions")
        if isinstance(versions, list) and versions:
            vurl = (versions[0] or {}).get("url")
            if vurl:
                return IgSlide(kind="video", url=_clean_url(str(vurl)))
        # Video without a playable URL — skip poster (yt-dlp / other parsers)
        return None
    # Prefer full-res candidates over display_url (often cropped for feed)
    versions = node.get("image_versions2")
    if isinstance(versions, dict):
        url = _best_image_candidate_url(versions.get("candidates") or [])
        if url:
            return IgSlide(kind="image", url=url)
    resources = node.get("display_resources")
    if isinstance(resources, list) and resources:
        url = _best_display_resource_url(resources)
        if url:
            return IgSlide(kind="image", url=url)
    display = node.get("display_url") or node.get("display_src")
    if display and not _skip_image(str(display)):
        return IgSlide(kind="image", url=_clean_url(str(display)))
    return None


def _dedupe_slides(slides: list[IgSlide]) -> list[IgSlide]:
    seen: set[str] = set()
    out: list[IgSlide] = []
    for s in slides:
        if not s or not s.url:
            continue
        key = s.url.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _page_title(text: str) -> str:
    match = _OG_TITLE_RE.search(text)
    if match:
        return html.unescape(match.group(1)).strip()
    match = re.search(r"<title[^>]*>([^<]+)</title>", text, re.I)
    if match:
        return html.unescape(match.group(1)).strip()
    return ""


def _skip_image(url: str) -> bool:
    lower = url.lower()
    return any(h in lower for h in _SKIP_IMAGE_HINTS)


def _clean_url(raw: str) -> str:
    url = html.unescape(raw).strip()
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    url = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), url)
    return url


def _fix_json(raw: str) -> str:
    return (
        raw.replace("\\/", "/")
        .replace("\\u0026", "&")
    )


def _cleanup_dir(directory: Path) -> None:
    try:
        if not directory.exists() or directory == DOWNLOAD_DIR:
            return
        for item in directory.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
        directory.rmdir()
    except OSError:
        pass
