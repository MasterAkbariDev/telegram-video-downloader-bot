"""Admin download/upload speed test."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path

import httpx

from telegram.constants import ParseMode

from bot.config import YTDLP_PROXY, get_max_file_size
from bot.downloader import MediaResult
from bot.messages import esc, format_size
from bot.uploader import send_media

logger = logging.getLogger(__name__)

# Cloudflare speed test endpoint (5 MB)
TEST_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=5242880"
TEST_SIZE_BYTES = 5 * 1024 * 1024


async def run_speed_test(message, *, status_msg) -> str:
    """Download + upload a 5 MB test file; return HTML report."""
    lines = ["⚡ <b>Speed test</b>", ""]

    # --- Download ---
    await status_msg.edit_text(
        "\n".join(lines + ["⏳ <b>Download test</b> (5 MB from Cloudflare)…"]),
        parse_mode=ParseMode.HTML,
    )
    dest = Path(tempfile.mkdtemp()) / "speedtest.bin"
    dl_start = time.monotonic()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _download_test_file(dest),
        )
    except Exception as exc:
        logger.exception("Speed test download failed")
        return "\n".join(lines + [f"❌ Download failed: {esc(str(exc))}"])

    dl_seconds = max(time.monotonic() - dl_start, 0.001)
    dl_bytes = dest.stat().st_size
    dl_speed = dl_bytes / dl_seconds
    lines.append(
        f"⬇️ Download: <b>{format_size(int(dl_speed))}/s</b> "
        f"({format_size(dl_bytes)} in {dl_seconds:.1f}s)"
    )

    # --- Upload ---
    await status_msg.edit_text("\n".join(lines + ["", "⏳ <b>Upload test</b> to Telegram…"]), parse_mode=ParseMode.HTML)
    result = MediaResult(
        title="Speed test",
        is_audio=False,
        file_size=dl_bytes,
        file_path=dest,
        used_direct=False,
    )
    caption = f"⚡ Speed test file ({format_size(dl_bytes)}) — safe to delete."

    upload_start = time.monotonic()
    upload_state = {"bytes": 0}

    def on_upload(done: int, total: int | None) -> None:
        upload_state["bytes"] = done

    try:
        await send_media(message, result, caption, progress_callback=on_upload)
    except Exception as exc:
        logger.exception("Speed test upload failed")
        dest.unlink(missing_ok=True)
        dest.parent.rmdir()
        lines.append(f"❌ Upload failed: {esc(str(exc))}")
        return "\n".join(lines)

    up_seconds = max(time.monotonic() - upload_start, 0.001)
    up_bytes = upload_state["bytes"] or dl_bytes
    up_speed = up_bytes / up_seconds
    lines.append(
        f"📤 Upload: <b>{format_size(int(up_speed))}/s</b> "
        f"({format_size(up_bytes)} in {up_seconds:.1f}s)"
    )
    lines.extend(["", "✅ Speed test complete."])

    dest.unlink(missing_ok=True)
    try:
        dest.parent.rmdir()
    except OSError:
        pass

    return "\n".join(lines)


def _download_test_file(dest: Path) -> None:
    if TEST_SIZE_BYTES > get_max_file_size():
        raise RuntimeError("Test file exceeds configured upload limit.")

    with httpx.Client(proxy=YTDLP_PROXY, timeout=120.0, follow_redirects=True) as client:
        with client.stream("GET", TEST_DOWNLOAD_URL) as resp:
            resp.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in resp.iter_bytes(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
