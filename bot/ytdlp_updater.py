"""Automatic yt-dlp updates.

yt-dlp releases new versions frequently, often specifically to fix a single
site's extractor after that site changes its player markup — exactly the
XVideos breakage found and worked around separately in bot/fallback.py this
session. yt-dlp remains the primary path for YouTube (where it's by far the
best-maintained extractor) and the fallback everywhere else, so keeping it
current matters. Checking PyPI periodically and pip-upgrading keeps that
current without needing a full git-based bot update.

Since Python doesn't hot-reload an already-imported package, a successful
upgrade needs the process to restart to actually take effect — done via
os.execv() re-exec in place (works regardless of how the process was
started — systemd, plain nohup, Docker — unlike shelling out to
`systemctl restart`, which only helps under systemd). Must re-exec with an
explicit `-m bot`, not `sys.argv`: for a `-m module` invocation, sys.argv[0]
resolves to bot/__main__.py's own file path, and re-execing *that* directly
as a bare script (rather than via `-m`) breaks the package-relative absolute
imports (`from bot.admin import ...`) used throughout this codebase.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

import httpx
from telegram.constants import ParseMode
from telegram.ext import Application

from bot import config as cfg
from bot.messages import esc

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SEC = 24 * 3600  # yt-dlp doesn't release often enough to need more
_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def _installed_version() -> str:
    import yt_dlp

    return yt_dlp.version.__version__


def _parse_calver(version: str) -> tuple[int, ...]:
    """yt-dlp uses YYYY.MM.DD[.patch] versions. Its own __version__ is
    zero-padded (e.g. "2026.08.19") but PyPI's JSON API reports the
    PEP 440-normalized form without zero-padding ("2026.8.19") — comparing
    those two as plain strings would treat the identical version as
    different and think an update is always available, restarting the bot
    every cycle for nothing. Parse both into an int tuple instead."""
    parts = []
    for segment in version.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _latest_pypi_version() -> str | None:
    try:
        resp = httpx.get(_PYPI_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except Exception as exc:
        logger.debug("Could not check PyPI for the latest yt-dlp version: %s", exc)
        return None


def _run_pip_upgrade() -> tuple[bool, str]:
    """Returns (success, output tail)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default,curl-cffi]"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output[-1500:]
    except Exception as exc:
        return False, str(exc)


async def ytdlp_update_loop(app: Application, interval_sec: int = _CHECK_INTERVAL_SEC) -> None:
    """Runs for the life of the process — periodically upgrades yt-dlp via
    pip and restarts the bot in place to pick it up."""
    while True:
        try:
            await _check_and_update_once(app)
        except Exception:
            logger.exception("yt-dlp auto-update check failed")
        await asyncio.sleep(interval_sec)


async def _check_and_update_once(app: Application) -> None:
    installed = _installed_version()
    latest = await asyncio.to_thread(_latest_pypi_version)
    if not latest or _parse_calver(latest) <= _parse_calver(installed):
        return

    logger.info("yt-dlp %s available (installed: %s) — upgrading", latest, installed)
    success, output = await asyncio.to_thread(_run_pip_upgrade)

    if not success:
        logger.warning("yt-dlp auto-update to %s failed: %s", latest, output)
        await _notify_admins(
            app,
            f"⚠️ <b>yt-dlp auto-update failed</b>\n\nTried {installed} → {latest}.\n"
            f"<pre>{esc(output[:500])}</pre>",
        )
        return

    await _notify_admins(
        app,
        f"✅ <b>yt-dlp updated</b>: <code>{installed}</code> → <code>{latest}</code>\n\nRestarting to apply it…",
    )
    logger.info("Restarting to load upgraded yt-dlp %s", latest)
    await asyncio.sleep(1.0)  # let the notification actually send before the process image changes
    os.execv(sys.executable, [sys.executable, "-m", "bot"])


async def _notify_admins(app: Application, text: str) -> None:
    for admin_id in cfg.ADMIN_IDS:
        try:
            await app.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.debug("Could not notify admin %s about yt-dlp update: %s", admin_id, exc)
