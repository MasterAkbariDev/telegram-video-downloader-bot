"""Run update.sh and notify admin after bot restart."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telegram.constants import ParseMode
from telegram.ext import Application

from bot.config import DATA_DIR, ROOT_DIR
from bot.messages import esc
from bot.version import format_version_block, format_version_label, strip_ansi

logger = logging.getLogger(__name__)

UPDATE_SCRIPT = ROOT_DIR / "update.sh"
PENDING_UPDATE_FILE = DATA_DIR / "pending_update.json"

_RESTART_EXIT_CODES = frozenset(
    {
        -signal.SIGTERM,
        -signal.SIGKILL,
        128 + signal.SIGTERM,
        128 + signal.SIGKILL,
    }
)

_STATUS_ICONS = {
    "progress": "⏳",
    "ok": "✅",
    "warn": "⚠️",
    "error": "❌",
}

_MESSAGE_ICONS = (
    ("fetch", "📡"),
    ("sync", "📥"),
    ("pull", "📥"),
    ("reset", "↩️"),
    ("diverg", "↩️"),
    ("dependenc", "📦"),
    ("restart", "🔄"),
    ("complete", "🎉"),
    ("up to date", "✅"),
    ("updated", "✅"),
)


@dataclass
class UpdateStep:
    status: str
    message: str


@dataclass
class UpdateState:
    steps: list[UpdateStep] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)

    def add_line(self, line: str) -> UpdateStep | None:
        self.raw_lines.append(line)
        step = _parse_step_line(line)
        if step:
            if self.steps and self.steps[-1].status == "progress" and step.status == "ok":
                self.steps[-1] = step
            else:
                self.steps.append(step)
        return step

    def icon_for(self, step: UpdateStep) -> str:
        lower = step.message.lower()
        if step.status == "error":
            return "❌"
        if step.status == "warn":
            return "⚠️"
        if step.status == "ok":
            if "complete" in lower:
                return "🎉"
            return "✅"
        for needle, icon in _MESSAGE_ICONS:
            if needle in lower:
                return icon
        return "⏳"


def schedule_update_notification(
    chat_id: int,
    user_id: int,
    *,
    status_message_id: int | None = None,
) -> None:
    payload = {
        "chat_id": chat_id,
        "user_id": user_id,
        "status_message_id": status_message_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    PENDING_UPDATE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def _parse_step_line(line: str) -> UpdateStep | None:
    if not line.startswith("::step::"):
        return None
    parts = line.split("::", 3)
    if len(parts) < 4:
        return None
    return UpdateStep(status=parts[2], message=parts[3])


def _format_update_status(state: UpdateState, *, footer: str | None = None) -> str:
    lines = ["🔄 <b>Bot update</b>", ""]
    if state.steps:
        for step in state.steps:
            icon = state.icon_for(step)
            lines.append(f"{icon} {esc(step.message)}")
    else:
        lines.append("⏳ Starting…")

    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)


def _update_output_indicates_success(output: str) -> bool:
    clean = strip_ansi(output).lower()
    return any(
        marker in clean
        for marker in (
            "update complete",
            "restart scheduled",
            "service restarted",
            "dependencies updated",
            "::step::ok::update complete",
        )
    )


def _exit_means_service_restart(returncode: int | None, output: str) -> bool:
    if returncode in _RESTART_EXIT_CODES:
        return _update_output_indicates_success(output) or "restart" in strip_ansi(output).lower()
    return False


async def _edit_status(
    app: Application,
    chat_id: int,
    message_id: int | None,
    text: str,
) -> None:
    if not message_id:
        return
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug("Could not edit update status message: %s", exc)


async def run_update_script(
    app: Application,
    chat_id: int,
    status_message_id: int | None = None,
) -> None:
    """Run update.sh with live step status; deferred restart lets this finish cleanly."""
    if not UPDATE_SCRIPT.is_file():
        PENDING_UPDATE_FILE.unlink(missing_ok=True)
        text = "❌ <b>Update failed</b>\n\nUpdate script not found on the server."
        if status_message_id:
            await _edit_status(app, chat_id, status_message_id, text)
        else:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        return

    state = UpdateState()
    await _edit_status(
        app,
        chat_id,
        status_message_id,
        _format_update_status(state, footer="⏳ Running update…"),
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(UPDATE_SCRIPT),
            cwd=str(ROOT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        logger.exception("Could not start update script")
        PENDING_UPDATE_FILE.unlink(missing_ok=True)
        await _edit_status(
            app,
            chat_id,
            status_message_id,
            f"❌ <b>Update failed</b>\n\n{esc(str(exc))}",
        )
        return

    assert proc.stdout is not None
    last_edit = 0.0
    output_parts: list[str] = []

    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode(errors="replace").rstrip("\n")
        output_parts.append(line)
        state.add_line(line)

        now = asyncio.get_running_loop().time()
        if now - last_edit >= 0.8:
            await _edit_status(app, chat_id, status_message_id, _format_update_status(state))
            last_edit = now

    returncode = await proc.wait()
    output = "\n".join(output_parts)

    if returncode == 0:
        await _edit_status(
            app,
            chat_id,
            status_message_id,
            _format_update_status(
                state,
                footer="🔄 Bot restarting… you will get a confirmation here shortly.",
            ),
        )
        return

    if _exit_means_service_restart(returncode, output):
        logger.info("Update stopped by restart signal (exit %s)", returncode)
        await _edit_status(
            app,
            chat_id,
            status_message_id,
            _format_update_status(
                state,
                footer="🔄 Bot restarting… you will get a confirmation here shortly.",
            ),
        )
        return

    PENDING_UPDATE_FILE.unlink(missing_ok=True)
    tail = strip_ansi(output)[-2500:]
    await _edit_status(
        app,
        chat_id,
        status_message_id,
        _format_update_status(state, footer=f"❌ <b>Failed</b> (exit {returncode})\n<pre>{esc(tail)}</pre>"),
    )


async def notify_pending_update(app: Application) -> None:
    if not PENDING_UPDATE_FILE.is_file():
        return

    try:
        payload = json.loads(PENDING_UPDATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Invalid pending update file: %s", exc)
        PENDING_UPDATE_FILE.unlink(missing_ok=True)
        return

    PENDING_UPDATE_FILE.unlink(missing_ok=True)
    chat_id = payload.get("chat_id")
    if not chat_id:
        return

    message_id = payload.get("status_message_id")
    text = (
        f"🎉 <b>Updated — {format_version_label()}</b>\n\n"
        f"{format_version_block(include_ytdlp=True)}\n\n"
        "✅ Restart complete. The bot is ready."
    )

    try:
        from bot.update_check import clear_notified_if_installed

        clear_notified_if_installed()
    except Exception:
        pass

    try:
        if message_id:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Could not send update notification to %s: %s", chat_id, exc)
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
