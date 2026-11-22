"""Download public media fixtures and optionally verify delivery to a bot admin."""

import argparse
import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image
from telegram import Bot
from telegram.request import HTTPXRequest
from yt_dlp.plugins import load_all_plugins

from bot import config
from bot.downloader import cleanup_file, resolve_media
from bot.uploader import send_media
from bot.urls import extract_urls


CASES = {
    "instagram-reel": ("https://www.instagram.com/reel/DdRyQxKteC1/", ("video",)),
    "instagram-image": ("https://www.instagram.com/p/DcwApJ2SUNd/", ("image",)),
    "instagram-video-post": ("https://www.instagram.com/p/Df3NCpmIkL1/", ("video",)),
    "instagram-image-carousel": ("https://www.instagram.com/p/BoHk1haB5tM/", ("image",) * 5),
    "instagram-video-carousel": ("https://www.instagram.com/p/BQ0eAlwhDrw/", ("video",) * 3),
    "instagram-mixed-carousel": (
        "https://www.instagram.com/p/CtohvHxLnWO/", ("image", "image", "video", "video"),
    ),
    "x-video": ("https://x.com/NASA/status/2094798955074781450", ("video",)),
    "x-image": ("https://x.com/NASA/status/2117633046002225363", ("image",)),
    "pinterest-image": ("https://co.pinterest.com/pin/824721750502199491/", ("image",)),
    "pinterest-video": ("https://www.pinterest.com/pin/1084663891475263837/", ("video",)),
}


def _download_for_validation(url, path):
    written = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=20) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            for chunk in response.iter_bytes(128 * 1024):
                written += len(chunk)
                if written > 100 * 1024 * 1024:
                    raise ValueError("Smoke-test download exceeds 100 MiB.")
                output.write(chunk)


def _resolve_and_validate(url, expected):
    extracted = extract_urls(url)
    if extracted != [url]:
        raise ValueError("Message URL extraction failed.")
    result = resolve_media(extracted[0])
    try:
        if result.album:
            items = [(item.kind, item.path, item.url) for item in result.album]
        else:
            kind = "image" if result.is_image else "audio" if result.is_audio else "video"
            items = [(kind, result.file_path, result.direct_url)]
        actual = tuple(kind for kind, _, _ in items)
        if actual != expected:
            raise ValueError(f"Media mismatch. Expected {expected}, received {actual}.")

        sizes = []
        with tempfile.TemporaryDirectory(prefix="media-smoke-") as temp:
            for index, (kind, path, direct_url) in enumerate(items):
                if path is None:
                    path = Path(temp) / str(index)
                    _download_for_validation(direct_url, path)
                if kind == "image":
                    with Image.open(path) as image:
                        image.verify()
                else:
                    stream = {"video": "v:0", "audio": "a:0"}[kind]
                    info = subprocess.run(
                        ["ffprobe", "-v", "error", "-select_streams", stream,
                         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                        check=True, capture_output=True, text=True, timeout=20,
                    )
                    if kind not in info.stdout.splitlines():
                        raise ValueError("Expected media stream missing.")
                sizes.append(path.stat().st_size)
        return result, sizes
    except Exception:
        _cleanup(result)
        raise


def _cleanup(result):
    files = [item.path for item in result.album] if result.album else [result.file_path]
    for path in {path.parent for path in files if path is not None}:
        cleanup_file(path)


async def _run(cases, bot=None, chat_id=None):
    anchor = None
    if bot is not None:
        anchor = await bot.send_message(
            chat_id=chat_id,
            text="Telegram downloader production smoke test. Verified media replies follow.",
            disable_notification=True,
        )
    failed = False
    for name, (url, expected) in cases.items():
        result = None
        try:
            result, sizes = await asyncio.to_thread(_resolve_and_validate, url, expected)
            if anchor is not None:
                receipt = await send_media(anchor, result, f"Smoke test • {name}")
                if isinstance(receipt, list):
                    if tuple(item["kind"] for item in receipt) != expected:
                        raise ValueError("Telegram album receipt did not match expected items.")
                elif not receipt or len(expected) != 1:
                    raise ValueError("Missing Telegram delivery receipt.")
            stage = "delivered" if anchor is not None else "validated"
            print(f"PASS {name} {stage} {list(zip(expected, sizes))}", flush=True)
        except Exception as exc:
            failed = True
            print(f"FAIL {name} {type(exc).__name__}", flush=True)
        finally:
            if result is not None:
                _cleanup(result)
    return int(failed)


async def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--url", action="append", nargs=3, metavar=("NAME", "URL", "KINDS"))
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--chat-id", type=int)
    args = parser.parse_args()
    cases = {}
    if args.case or not args.url:
        cases = {name: CASES[name] for name in (args.case or CASES)}
    for name, url, kinds in args.url or []:
        expected = tuple(kinds.split(","))
        if not all(kind in {"image", "video", "audio"} for kind in expected):
            parser.error("KINDS must be comma-separated image, video, or audio values.")
        cases[name] = (url, expected)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_all_plugins()
    if not (args.deliver or args.chat_id is not None):
        return await _run(cases)
    chat_id = args.chat_id
    if chat_id is None and len(config.ADMIN_IDS) == 1:
        chat_id = next(iter(config.ADMIN_IDS))
    if chat_id not in config.ADMIN_IDS:
        parser.error("Provide a configured admin chat ID for delivery.")
    if not config.BOT_TOKEN:
        parser.error("Delivery requires the configured bot token.")
    request = HTTPXRequest(proxy=config.TELEGRAM_PROXY, connect_timeout=15, read_timeout=30, write_timeout=90)
    try:
        async with Bot(config.BOT_TOKEN, request=request) as bot:
            return await _run(cases, bot, chat_id)
    except Exception as exc:
        print(f"FAIL telegram_connect {type(exc).__name__}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
