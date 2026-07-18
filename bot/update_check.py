"""Check GitHub for newer releases and notify admins once per version."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass

import httpx
from telegram.constants import ParseMode
from telegram.ext import Application

from bot.changelog import changelog_since
from bot.config import ADMIN_IDS, DATA_DIR, ROOT_DIR, TELEGRAM_PROXY, YTDLP_PROXY
from bot.messages import esc
from bot.version import get_version

logger = logging.getLogger(__name__)

_ALERT_STATE_FILE = DATA_DIR / "update_alert.json"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")

# Raw VERSION mirrors (cache-busted at request time)
_VERSION_URLS = (
    "https://raw.githubusercontent.com/MasterAkbariDev/telegram-video-downloader-bot/main/VERSION",
    "https://cdn.jsdelivr.net/gh/MasterAkbariDev/telegram-video-downloader-bot@main/VERSION",
)
_CHANGELOG_URLS = (
    "https://raw.githubusercontent.com/MasterAkbariDev/telegram-video-downloader-bot/main/CHANGELOG.md",
    "https://cdn.jsdelivr.net/gh/MasterAkbariDev/telegram-video-downloader-bot@main/CHANGELOG.md",
)

# Default: check every hour
UPDATE_CHECK_INTERVAL_SEC = 3600


@dataclass
class UpdateCheckResult:
    local_version: str
    remote_version: str | None
    update_available: bool
    remote_changelog: str | None = None
    error: str | None = None
    source: str | None = None


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.strip().lstrip("vV").split("."):
        digits = re.match(r"(\d+)", piece)
        parts.append(int(digits.group(1)) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _parse_version_line(text: str) -> str | None:
    if not text:
        return None
    for line in text.strip().splitlines():
        line = line.strip().lstrip("vV")
        if _VERSION_RE.match(line):
            return _VERSION_RE.match(line).group(0)
    return None


def _load_alert_state() -> dict:
    if not _ALERT_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(_ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_alert_state(state: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _ALERT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def already_notified(remote_version: str) -> bool:
    state = _load_alert_state()
    return state.get("notified_remote_version") == remote_version


def mark_notified(remote_version: str) -> None:
    state = _load_alert_state()
    state["notified_remote_version"] = remote_version
    state["notified_at"] = time.time()
    _save_alert_state(state)


def clear_notified_if_installed() -> None:
    """Clear alert marker once local version has caught up."""
    state = _load_alert_state()
    notified = state.get("notified_remote_version")
    if not notified:
        return
    get_version.cache_clear()
    local = get_version()
    if _version_tuple(local) >= _version_tuple(str(notified)):
        state.pop("notified_remote_version", None)
        _save_alert_state(state)
        logger.info("Cleared update alert marker (local=%s caught up to %s)", local, notified)


def _remote_version_via_git() -> str | None:
    """Prefer git fetch — same path update.sh uses."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--depth", "1"],
            cwd=ROOT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        out = subprocess.check_output(
            ["git", "show", "origin/main:VERSION"],
            cwd=ROOT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return _parse_version_line(out)
    except Exception as exc:
        logger.debug("git remote VERSION failed: %s", exc)
        return None


def _http_get_text(url: str, *, proxy: str | None) -> str | None:
    bust = f"{url}{'&' if '?' in url else '?'}t={int(time.time())}"
    try:
        with httpx.Client(
            timeout=20.0,
            proxy=proxy,
            follow_redirects=True,
            headers={
                "User-Agent": "telegram-video-downloader-bot/update-check",
                "Cache-Control": "no-cache",
            },
        ) as client:
            resp = client.get(bust)
            if resp.status_code >= 400:
                return None
            return resp.text
    except Exception as exc:
        logger.debug("HTTP get %s via proxy=%s failed: %s", url, bool(proxy), exc)
        return None


def _fetch_remote_version_http() -> tuple[str | None, str | None]:
    """Returns (version, changelog_text)."""
    proxies = []
    for p in (None, YTDLP_PROXY, TELEGRAM_PROXY):
        if p not in proxies:
            proxies.append(p)

    version = None
    for proxy in proxies:
        for url in _VERSION_URLS:
            text = _http_get_text(url, proxy=proxy)
            parsed = _parse_version_line(text or "")
            if parsed:
                version = parsed
                break
        if version:
            break

    changelog = None
    if version:
        for proxy in proxies:
            for url in _CHANGELOG_URLS:
                text = _http_get_text(url, proxy=proxy)
                if text and "## " in text:
                    changelog = text
                    break
            if changelog:
                break

    return version, changelog


def fetch_update_check() -> UpdateCheckResult:
    get_version.cache_clear()
    local = get_version()

    remote = _remote_version_via_git()
    source = "git" if remote else None
    changelog_text = None

    if not remote:
        remote, changelog_text = _fetch_remote_version_http()
        source = "http" if remote else None

    if not remote:
        return UpdateCheckResult(
            local_version=local,
            remote_version=None,
            update_available=False,
            error="Could not read remote VERSION from GitHub (git/http failed)",
        )

    if changelog_text is None:
        _, changelog_text = _fetch_remote_version_http()

    newer = _version_tuple(remote) > _version_tuple(local)
    logger.info(
        "Update check: local=%s remote=%s available=%s source=%s",
        local,
        remote,
        newer,
        source,
    )
    return UpdateCheckResult(
        local_version=local,
        remote_version=remote,
        update_available=newer,
        remote_changelog=changelog_text,
        source=source,
    )


def format_update_available_message(result: UpdateCheckResult) -> str:
    remote = result.remote_version or "?"
    notes = changelog_since(
        result.local_version,
        result.remote_changelog,
    )
    return (
        "🆕 <b>Update available</b>\n\n"
        f"Running: <b>v{esc(result.local_version)}</b>\n"
        f"Latest: <b>v{esc(remote)}</b>\n\n"
        f"{notes}\n\n"
        "Open /admin → <b>Update bot</b> to install."
    )


def format_up_to_date_message(result: UpdateCheckResult) -> str:
    remote = result.remote_version or result.local_version
    return (
        f"✅ <b>You’re up to date</b>\n\n"
        f"Running: <b>v{esc(result.local_version)}</b>\n"
        f"Latest: <b>v{esc(remote)}</b>"
    )


def format_update_panel_message(result: UpdateCheckResult) -> str:
    """Shown on the Update bot admin screen."""
    if result.error:
        return (
            "🔄 <b>Update bot</b>\n\n"
            f"⚠️ Couldn’t check for updates: {esc(result.error)}\n\n"
            "You can still run an update to sync."
        )
    if result.update_available:
        remote = result.remote_version or "?"
        notes = changelog_since(result.local_version, result.remote_changelog)
        return (
            "🔄 <b>Update bot</b>\n\n"
            f"🆕 <b>v{esc(result.local_version)}</b> → <b>v{esc(remote)}</b>\n\n"
            f"{notes}\n\n"
            "This will download the latest code, update packages, and restart the bot."
        )
    remote = result.remote_version or result.local_version
    return (
        "🔄 <b>Update bot</b>\n\n"
        f"✅ Already on the latest version: <b>v{esc(remote)}</b>\n\n"
        "You can still run an update to re-sync."
    )


async def notify_admins_if_update_available(app: Application) -> None:
    """Alert each admin at most once per remote version."""
    if not ADMIN_IDS:
        return

    clear_notified_if_installed()
    result = await asyncio.to_thread(fetch_update_check)
    if result.error:
        logger.info("Update check skipped: %s", result.error)
        return
    if not result.update_available or not result.remote_version:
        logger.info(
            "Update check: local=%s remote=%s (up to date)",
            result.local_version,
            result.remote_version,
        )
        return
    if already_notified(result.remote_version):
        logger.info(
            "Update %s available but admins already notified",
            result.remote_version,
        )
        return

    text = format_update_available_message(result)
    sent_any = False
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_any = True
        except Exception as exc:
            logger.warning("Could not send update alert to admin %s: %s", admin_id, exc)

    if sent_any:
        mark_notified(result.remote_version)
        logger.info("Notified admins about update v%s", result.remote_version)


async def update_check_loop(app: Application, interval_sec: int = UPDATE_CHECK_INTERVAL_SEC) -> None:
    """Background loop: check periodically; alert once per new version."""
    # Startup already checks once; wait an interval before the next pass.
    while True:
        try:
            await asyncio.sleep(interval_sec)
            await notify_admins_if_update_available(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic update check failed")
