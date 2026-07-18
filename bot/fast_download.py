"""Parallel HTTP downloads for CDN direct URLs (yt-dlp + fallback)."""

from __future__ import annotations

import http.cookiejar
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from bot.config import YTDLP_PROXY, get_cookies_file, get_max_file_size
from bot.messages import download_progress

try:
    from bot.jobs import CancelCheck
except ImportError:  # pragma: no cover
    CancelCheck = Callable[[], None]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PARALLEL_MIN_BYTES = 2 * 1024 * 1024
PARALLEL_WORKERS = 8
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)


def load_cookies() -> httpx.Cookies:
    path = get_cookies_file()
    cookies = httpx.Cookies()
    if not path:
        return cookies
    jar = http.cookiejar.MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    except Exception as exc:
        logger.warning("Could not load cookies for download: %s", exc)
    return cookies


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"},
        cookies=load_cookies(),
        proxy=YTDLP_PROXY,
        timeout=30.0,
        follow_redirects=True,
    )


def build_headers(
    referer: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    if extra:
        headers.update(extra)
    return headers


def head_size(client: httpx.Client, url: str, headers: dict[str, str]) -> int | None:
    try:
        resp = client.head(url, headers=headers, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code >= 400:
            return None
        return _total_size_from_headers(resp.headers) or _parse_int_header(
            resp.headers.get("content-length")
        )
    except httpx.HTTPError:
        return None


def supports_range(client: httpx.Client, url: str, headers: dict[str, str]) -> bool:
    try:
        resp = client.head(url, headers=headers, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code >= 400:
            return False
        ranges = (resp.headers.get("accept-ranges") or "").lower()
        return ranges == "bytes" or "content-range" in resp.headers
    except httpx.HTTPError:
        return False


def download_http(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    """Download a direct HTTP(S) file — parallel range requests when supported."""
    from bot.downloader import FileTooLargeError

    headers = build_headers(referer, extra_headers)
    total = head_size(client, url, headers)

    if total and total > get_max_file_size():
        raise FileTooLargeError(total)

    if total and total >= PARALLEL_MIN_BYTES and supports_range(client, url, headers):
        logger.info("Parallel download (%d workers, %s bytes)", PARALLEL_WORKERS, total)
        _download_parallel(url, dest, total, headers, progress_callback, cancel_check)
        return

    _download_sequential(client, url, dest, headers, progress_callback, total, cancel_check)


def ytdlp_http_candidate(info: dict) -> tuple[str, str] | None:
    """Return (media_url, ext) when yt-dlp info is a plain HTTP file (not HLS/DASH/merge)."""
    if info.get("fragments") or info.get("manifest_url"):
        return None

    protocol = (info.get("protocol") or "").lower()
    if any(token in protocol for token in ("m3u8", "dash", "ism", "f4m")):
        return None

    requested = info.get("requested_formats") or []
    if len(requested) > 1:
        return None

    media_url = info.get("url")
    if not media_url or not str(media_url).startswith(("http://", "https://")):
        return None

    # Skip audio-only streams (Instagram can expose these separately)
    vcodec = info.get("vcodec")
    if vcodec is not None and str(vcodec).lower() in {"none", "null", ""}:
        return None

    ext = (info.get("ext") or "mp4").lower()
    if ext in {"m3u8", "mpd", "f4m", "webm_frag", "mp3", "m4a", "opus", "ogg"}:
        return None

    return str(media_url), ext


def ytdlp_http_headers(info: dict) -> dict[str, str]:
    raw = info.get("http_headers") or {}
    return {str(k): str(v) for k, v in raw.items()}


def _download_sequential(
    client: httpx.Client,
    url: str,
    dest: Path,
    headers: dict[str, str],
    progress_callback: ProgressCallback | None,
    total: int | None,
    cancel_check: CancelCheck | None,
) -> None:
    from bot.downloader import FileTooLargeError

    last_report = 0.0
    downloaded = 0
    with client.stream("GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        if not total:
            total = _parse_int_header(resp.headers.get("content-length"))
        with dest.open("wb") as handle:
            for chunk in resp.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                if cancel_check:
                    cancel_check()
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > get_max_file_size():
                    raise FileTooLargeError(downloaded)
                handle.write(chunk)
                if progress_callback and total:
                    now = time.monotonic()
                    if now - last_report >= 1.5:
                        progress_callback(
                            download_progress(downloaded, total, max_bytes=get_max_file_size())
                        )
                        last_report = now


def _download_parallel(
    url: str,
    dest: Path,
    total: int,
    headers: dict[str, str],
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> None:
    from bot.downloader import FileTooLargeError

    workers = min(PARALLEL_WORKERS, max(2, total // (512 * 1024)))
    part_size = (total + workers - 1) // workers
    parts: list[tuple[int, int, int]] = []
    for index in range(workers):
        start = index * part_size
        if start >= total:
            break
        end = min(start + part_size - 1, total - 1)
        parts.append((index, start, end))

    temp_dir = dest.parent
    progress = {"bytes": 0, "lock": threading.Lock(), "last_report": 0.0}

    def fetch_part(part: tuple[int, int, int]) -> Path:
        index, start, end = part
        part_path = temp_dir / f"{dest.name}.part{index}"
        part_headers = {**headers, "Range": f"bytes={start}-{end}"}
        with httpx.Client(
            headers={"User-Agent": DESKTOP_UA},
            proxy=YTDLP_PROXY,
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as part_client:
            with part_client.stream("GET", url, headers=part_headers) as resp:
                resp.raise_for_status()
                with part_path.open("wb") as handle:
                    for chunk in resp.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        if cancel_check:
                            cancel_check()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        if progress_callback:
                            with progress["lock"]:
                                progress["bytes"] += len(chunk)
                                now = time.monotonic()
                                if now - progress["last_report"] >= 1.5:
                                    progress_callback(
                                        download_progress(
                                            progress["bytes"],
                                            total,
                                            max_bytes=get_max_file_size(),
                                        )
                                    )
                                    progress["last_report"] = now
        return part_path

    part_files: list[Path] = []
    try:
        with ThreadPoolExecutor(max_workers=len(parts)) as pool:
            futures = [pool.submit(fetch_part, part) for part in parts]
            for future in as_completed(futures):
                part_files.append(future.result())

        part_files.sort(key=lambda p: int(p.name.rsplit("part", 1)[-1]))
        written = 0
        with dest.open("wb") as out:
            for part_path in part_files:
                with part_path.open("rb") as part:
                    while True:
                        chunk = part.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > get_max_file_size():
                            raise FileTooLargeError(written)
                        out.write(chunk)
    finally:
        for part_path in part_files:
            part_path.unlink(missing_ok=True)


def _parse_int_header(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _total_size_from_headers(headers: httpx.Headers) -> int | None:
    content_range = headers.get("content-range")
    if content_range and "/" in content_range:
        total_part = content_range.split("/")[-1]
        return _parse_int_header(total_part)
    return _parse_int_header(headers.get("content-length"))
