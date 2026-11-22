"""Stream public CDN media to bounded, validated local files."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from bot.config import YTDLP_PROXY
from bot.fast_download import DESKTOP_UA

_DOWNLOAD_SLOTS = threading.BoundedSemaphore(2)


def is_media_cdn(url: str, domains: tuple[str, ...]) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.port in {None, 443}
            and any(host == domain or host.endswith("." + domain)
                    for domain in domains)
        )
    except ValueError:
        return False


def download_slides(slides, directory, progress_callback, cancel_check, *, provider, domains, referer):
    while not _DOWNLOAD_SLOTS.acquire(timeout=0.25):
        if cancel_check:
            cancel_check()
    try:
        return _download_all(slides, directory, progress_callback, cancel_check, provider, domains, referer)
    finally:
        _DOWNLOAD_SLOTS.release()


def _download_all(slides, directory, progress_callback, cancel_check, provider, domains, referer):
    from bot.downloader import AlbumItem, FileTooLargeError, _maybe_compress
    from bot.config import STANDARD_UPLOAD_LIMIT, get_download_size_cap, get_max_file_size

    if len(slides) > 20:
        raise RuntimeError(f"{provider} returned too many items in one link.")
    if progress_callback:
        progress_callback(f"⬇️ <b>Downloading {len(slides)} {provider} media item(s)…</b>")
    album = []
    total_size = 0
    is_album = len(slides) > 1
    job_limit = min(get_download_size_cap(), 200 * 1024 * 1024) if is_album else get_download_size_cap()
    video_limit = STANDARD_UPLOAD_LIMIT if is_album else job_limit
    # Do not forward saved cookies to media CDNs. Check each redirect target.
    with httpx.Client(
        proxy=YTDLP_PROXY, timeout=18.0, follow_redirects=False,
        headers={"User-Agent": DESKTOP_UA},
    ) as client:
        for index, slide in enumerate(slides, start=1):
            if cancel_check:
                cancel_check()
            current_url = slide.url
            for _ in range(4):
                if not current_url or not is_media_cdn(current_url, domains):
                    raise RuntimeError(f"{provider} returned an unexpected media host.")
                with client.stream("GET", current_url, headers={"Referer": referer}) as response:
                    if response.is_redirect and "location" in response.headers:
                        current_url = urljoin(current_url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    extension = "mp4" if slide.kind == "video" else "image"
                    destination = directory / f"slide_{index:02d}.{extension}"
                    item_size = 0
                    item_limit = min(job_limit, 10 * 1024 * 1024 if slide.kind == "image" else video_limit)
                    with destination.open("wb") as output:
                        for chunk in response.iter_bytes(128 * 1024):
                            if cancel_check:
                                cancel_check()
                            total_size += len(chunk)
                            item_size += len(chunk)
                            if total_size > job_limit or item_size > item_limit:
                                raise FileTooLargeError(max(item_size, total_size))
                            output.write(chunk)
                    break
            else:
                raise RuntimeError(f"{provider} returned too many media redirects.")
            destination = validate_media_file(destination, slide.kind)
            if not is_album and slide.kind == "video" and destination.stat().st_size > get_max_file_size():
                destination = _maybe_compress(
                    destination, is_audio=False, progress_callback=progress_callback, cancel_check=cancel_check,
                )
            size = destination.stat().st_size
            if size > get_max_file_size() or (slide.kind == "image" and size > 10 * 1024 * 1024):
                raise FileTooLargeError(size)
            album.append(AlbumItem(kind=slide.kind, path=destination, file_size=size))
    return album


def validate_media_file(path: Path, kind: str) -> Path:
    if kind == "image":
        from PIL import Image

        with Image.open(path) as image:
            if image.width * image.height > 25_000_000:
                raise RuntimeError("The provider returned an oversized image.")
            image.verify()
            image_format = image.format
        if image_format in {"JPEG", "PNG"}:
            return path.rename(path.with_suffix(".jpg" if image_format == "JPEG" else ".png"))
        converted = path.with_suffix(".jpg")
        with Image.open(path) as image:
            image.convert("RGB").save(converted, format="JPEG", quality=92)
        path.unlink()
        return converted

    if not _file_looks_like_video(path):
        raise RuntimeError("The provider returned an invalid video file.")
    if ffprobe := shutil.which("ffprobe"):
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=12,
        )
        if probe.returncode or "video" not in probe.stdout.splitlines():
            raise RuntimeError("The provider returned a file without a valid video stream.")
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
