"""Instagram image / carousel extraction (yt-dlp is video-only)."""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import httpx

from bot.config import DOWNLOAD_DIR, YTDLP_PROXY, get_cookies_file
from bot.fast_download import DESKTOP_UA, download_http, make_client

try:
    from bot.jobs import CancelCheck
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

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


def is_instagram_url(url: str) -> bool:
    lower = url.lower()
    return "instagram.com" in lower or "instagr.am" in lower


def resolve_instagram_album(
    url: str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
):
    """
    Download Instagram photo / carousel posts.

    Returns MediaResult with album items, or None if this looks like a
    single-video reel that yt-dlp should handle.
    """
    from bot.downloader import AlbumItem, MediaResult

    # Reels / IGTV are always video — never scrape posters as photos
    if re.search(r"instagram\.com/(?:reel|tv)/", url, re.IGNORECASE):
        return None

    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("📸 <b>Checking for Instagram photos…</b>")

    _title, slides = scrape_instagram_slides(url)
    if not slides:
        return None

    images = [s for s in slides if s.kind == "image"]
    videos = [s for s in slides if s.kind == "video"]

    # Single video / reel → leave to yt-dlp
    if not images and len(videos) <= 1:
        return None

    if progress_callback:
        n = len(slides)
        kind = "photos" if not videos else "media"
        progress_callback(f"⬇️ <b>Downloading {n} Instagram {kind}…</b>")

    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    album: list[AlbumItem] = []
    try:
        with make_client() as client:
            for i, slide in enumerate(slides[:10], start=1):  # Telegram album max 10
                if cancel_check:
                    cancel_check()
                ext = "mp4" if slide.kind == "video" else "jpg"
                dest = output_dir / f"slide_{i:02d}.{ext}"
                accept = (
                    "video/mp4,video/*,*/*;q=0.8"
                    if slide.kind == "video"
                    # Prefer JPEG/PNG — Telegram media groups reject some WebP/AVIF
                    else "image/jpeg,image/jpg,image/png,image/*,*/*;q=0.8"
                )
                try:
                    download_http(
                        client,
                        slide.url,
                        dest,
                        referer=url,
                        extra_headers={"Accept": accept},
                        progress_callback=None,
                        cancel_check=cancel_check,
                    )
                except Exception as exc:
                    logger.warning("Instagram slide %s failed: %s", i, exc)
                    continue
                size = dest.stat().st_size
                if size < 2_000:
                    dest.unlink(missing_ok=True)
                    continue
                kind = slide.kind
                # Poster JPEGs must never be sent as "video" or mistaken for photos of a reel
                if kind == "video" and _file_looks_like_image(dest):
                    logger.warning("Dropping video slide that is actually an image poster")
                    dest.unlink(missing_ok=True)
                    continue
                if kind == "image" and _file_looks_like_video(dest):
                    kind = "video"
                    new_dest = dest.with_suffix(".mp4")
                    dest.rename(new_dest)
                    dest = new_dest
                if kind == "image":
                    dest = _ensure_telegram_photo(dest) or dest
                    size = dest.stat().st_size
                album.append(
                    AlbumItem(kind=kind, path=dest, url=slide.url, file_size=size)
                )
    except Exception:
        _cleanup_dir(output_dir)
        raise

    if not album:
        _cleanup_dir(output_dir)
        return None

    total_size = sum(a.file_size or 0 for a in album)
    only_images = all(a.kind == "image" for a in album)
    # Single leftover video → yt-dlp (better mux / quality)
    if not only_images and len(album) == 1 and album[0].kind == "video":
        _cleanup_dir(output_dir)
        return None

    logger.info(
        "Instagram album %s: %d item(s) (%s)",
        url[:80],
        len(album),
        "images" if only_images else "mixed",
    )
    return MediaResult(
        # Never use Instagram og:title (full caption + hashtags) as media title
        title="Instagram post",
        is_audio=False,
        file_size=total_size,
        file_path=album[0].path if len(album) == 1 else None,
        used_direct=False,
        is_image=only_images,
        # Single image: send as one photo (not a 1-item album)
        album=album if len(album) > 1 else None,
        uploader=_uploader_from_instagram_url(url),
    )


def scrape_instagram_slides(url: str) -> tuple[str, list[IgSlide]]:
    shortcode = _shortcode(url)
    page_urls = [url]
    if shortcode:
        page_urls.extend(
            [
                f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
                f"https://www.instagram.com/p/{shortcode}/embed/",
                f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis",
                f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
                f"https://www.instagram.com/p/{shortcode}/",
            ]
        )

    cookies = _load_cookie_header()
    header_sets = [
        {
            "User-Agent": _MOBILE_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        {
            "User-Agent": DESKTOP_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ]
    if cookies:
        for h in header_sets:
            h["Cookie"] = cookies

    title = ""
    best: list[IgSlide] = []

    with httpx.Client(
        headers={"User-Agent": DESKTOP_UA},
        proxy=YTDLP_PROXY,
        timeout=25.0,
        follow_redirects=True,
    ) as client:
        for page_url in page_urls:
            for headers in header_sets:
                try:
                    resp = client.get(page_url, headers=headers)
                    if resp.status_code >= 400:
                        continue
                    text = resp.text
                    # Some __a=1 responses are pure JSON
                    if text.lstrip().startswith("{"):
                        try:
                            data = json.loads(text)
                            slides = _slides_from_media_object(
                                data.get("graphql", {}).get("shortcode_media")
                                or data.get("items", [None])[0]
                                or data
                            )
                        except (json.JSONDecodeError, TypeError, IndexError):
                            slides = _slides_from_html(text)
                    else:
                        if not title:
                            title = _page_title(text)
                        slides = _slides_from_html(text)
                    if len(slides) > len(best):
                        best = slides
                    if len(best) >= 2:
                        return title, best
                except httpx.HTTPError as exc:
                    logger.debug("Instagram page fetch failed %s: %s", page_url, exc)

    return title, best


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
        slides = _slides_from_sidecar_obj(block)
        if slides:
            return _dedupe_slides(slides)

    # Newer carousel_media arrays (balanced brackets)
    for arr in _extract_json_arrays_after_key(text, "carousel_media"):
        if isinstance(arr, list) and arr:
            slides = _slides_from_carousel_media(arr)
            if slides:
                return _dedupe_slides(slides)

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
        slides = _slides_from_sidecar_obj(sidecar)
        if slides:
            return slides

    carousel = obj.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        slides = _slides_from_carousel_media(carousel)
        if slides:
            return slides

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
    return slides


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
    return slides


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
    is_video = bool(node.get("is_video") or node.get("__typename") == "GraphVideo")
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


def _file_looks_like_image(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    if head[:3] == b"\xff\xd8\xff":
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return False


def _ensure_telegram_photo(path: Path) -> Path | None:
    """Convert WebP/AVIF to JPEG so Telegram media groups accept the file."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None

    is_jpeg = head[:3] == b"\xff\xd8\xff"
    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    if is_jpeg or is_png:
        return path

    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    is_avif = b"ftypavif" in head or b"ftypavis" in head
    if not (is_webp or is_avif):
        return path

    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg missing — cannot convert %s for Telegram", path.name)
        return path

    out = path.with_suffix(".jpg")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        if out.is_file() and out.stat().st_size > 1000:
            path.unlink(missing_ok=True)
            logger.info("Converted %s → JPEG for Telegram album", path.name)
            return out
    except Exception as exc:
        logger.warning("Could not convert %s to JPEG: %s", path.name, exc)
        out.unlink(missing_ok=True)
    return path


def _file_looks_like_video(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    if head[:4] == b"\x1aE\xdf\xa3":  # webm/mkv
        return True
    return False


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


def _shortcode(url: str) -> str | None:
    match = re.search(r"instagram\.com/(?:reel|p|tv)/([^/?#]+)", url, re.IGNORECASE)
    return match.group(1) if match else None


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


def _uploader_from_instagram_url(url: str) -> str | None:
    match = re.search(r"instagram\.com/@([^/?#]+)", url, re.IGNORECASE)
    return match.group(1) if match else None


def _clean_url(raw: str) -> str:
    url = html.unescape(raw).strip()
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    url = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), url)
    return unquote(url)


def _fix_json(raw: str) -> str:
    return (
        raw.replace("\\/", "/")
        .replace("\\u0026", "&")
    )


def _load_cookie_header() -> str | None:
    path = get_cookies_file()
    if not path:
        return None
    try:
        jar = httpx.Cookies()
        import http.cookiejar

        mozilla = http.cookiejar.MozillaCookieJar(path)
        mozilla.load(ignore_discard=True, ignore_expires=True)
        parts = []
        for c in mozilla:
            if "instagram" in (c.domain or ""):
                parts.append(f"{c.name}={c.value}")
        return "; ".join(parts) if parts else None
    except Exception as exc:
        logger.debug("Could not load IG cookies: %s", exc)
        return None


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
