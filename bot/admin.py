"""Admin settings panel — stats, logs, 2 GB API credentials."""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import env_store, media_cache, stats
from bot.changelog import format_changelog_for_telegram
from bot.config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    is_admin,
    large_upload_enabled,
    reload_settings,
)
from bot.messages import esc, format_size
from bot.speedtest import run_speed_test
from bot.update_check import (
    fetch_update_check,
    format_update_panel_message,
)
from bot.uploader import reset_telethon_client, upload_limit_label
from bot.updater import run_update_script, schedule_update_notification
from bot.version import format_version_label

logger = logging.getLogger(__name__)

AWAIT_API_ID = "admin_await_api_id"
AWAIT_API_HASH = "admin_await_api_hash"

MY_TELEGRAM_ORG = "https://my.telegram.org/apps"


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistics", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Recent downloads", callback_data="admin:logs")],
            [InlineKeyboardButton("⚠️ Failures & requests", callback_data="admin:failures")],
            [InlineKeyboardButton("💾 Disk & storage", callback_data="admin:disk")],
            [InlineKeyboardButton("🗑 Clear media cache", callback_data="admin:cache")],
            [InlineKeyboardButton("📜 Changelog", callback_data="admin:changelog")],
            [InlineKeyboardButton("🔑 2 GB upload API", callback_data="admin:api")],
            [InlineKeyboardButton("⚡ Speed test", callback_data="admin:speedtest")],
            [InlineKeyboardButton("🔄 Update bot", callback_data="admin:update")],
            [InlineKeyboardButton("✕ Close", callback_data="admin:close")],
        ]
    )


def cache_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Clear all", callback_data="admin:cache_clear"),
                InlineKeyboardButton("✕ Cancel", callback_data="admin:home"),
            ]
        ]
    )


def update_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Run update", callback_data="admin:update_confirm"),
                InlineKeyboardButton("✕ Cancel", callback_data="admin:home"),
            ]
        ]
    )


def api_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📝 Set API ID", callback_data="admin:api_id")],
        [InlineKeyboardButton("🔐 Set API Hash", callback_data="admin:api_hash")],
    ]
    if large_upload_enabled():
        rows.append([InlineKeyboardButton("🗑 Remove 2 GB credentials", callback_data="admin:api_clear")])
    rows.append([InlineKeyboardButton("« Back", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def _require_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and is_admin(user.id)


def _require_admin_dm(update: Update) -> bool:
    return _require_admin(update) and _is_private_chat(update)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin_dm(update):
        if _require_admin(update) and not _is_private_chat(update):
            await update.message.reply_text(
                "⚙️ Admin panel is only available in a <b>private chat</b> with the bot.\n"
                "Open the bot in DM and send /admin there.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("⛔ Admin access only.")
        return
    await update.message.reply_text(
        _panel_header(),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin_dm(update):
        if _require_admin(update) and not _is_private_chat(update):
            await update.message.reply_text(
                "🔄 Updates are only available in a <b>private chat</b> with the bot.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("⛔ Admin access only.")
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    status_msg = await update.message.reply_text(
        "🔄 <b>Bot update</b>\n\n⏳ Starting…",
        parse_mode=ParseMode.HTML,
    )
    await start_bot_update(
        context.application,
        chat.id,
        user.id,
        status_message_id=status_msg.message_id,
        notify=False,
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _require_admin_dm(update):
        if _require_admin(update) and not _is_private_chat(update):
            await query.answer("Open the bot in DM to use the admin panel.", show_alert=True)
        else:
            await query.answer("Admin access only.", show_alert=True)
        return

    data = query.data or ""

    if data == "admin:close":
        await query.message.delete()
        return

    if data == "admin:home":
        await query.edit_message_text(
            _panel_header(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:stats":
        try:
            text = _stats_text()
        except Exception as exc:
            logger.exception("Stats error")
            text = f"📊 <b>Statistics</b>\n\n❌ Error loading stats: {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin:home")]]),
        )
        return

    if data == "admin:logs":
        try:
            text = _logs_text()
        except Exception as exc:
            logger.exception("Logs error")
            text = f"👥 <b>Recent downloads</b>\n\n❌ Error loading logs: {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin:home")]]),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:failures":
        try:
            text = _failures_text()
        except Exception as exc:
            logger.exception("Failures log error")
            text = f"⚠️ <b>Failures &amp; requests</b>\n\n❌ Error: {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Clear failures", callback_data="admin:failures_clear"
                        )
                    ],
                    [InlineKeyboardButton("« Back", callback_data="admin:home")],
                ]
            ),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:failures_clear":
        try:
            n = stats.clear_failures()
            text = f"⚠️ <b>Failures &amp; requests</b>\n\nCleared <b>{n}</b> log entries."
        except Exception as exc:
            logger.exception("Clear failures error")
            text = f"⚠️ <b>Failures &amp; requests</b>\n\n❌ {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data="admin:home")]]
            ),
        )
        return

    if data == "admin:disk":
        try:
            text = _disk_text()
        except Exception as exc:
            logger.exception("Disk stats error")
            text = f"💾 <b>Disk &amp; storage</b>\n\n❌ Error: {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin:home")]]),
        )
        return

    if data == "admin:cache":
        await query.edit_message_text(
            _cache_prompt_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=cache_confirm_keyboard(),
        )
        return

    if data == "admin:cache_clear":
        try:
            n = media_cache.clear_all()
            text = (
                "🗑 <b>Media cache cleared</b>\n\n"
                f"Removed <b>{n}</b> cached file_id(s).\n"
                "Next downloads will re-fetch and re-upload media."
            )
        except Exception as exc:
            logger.exception("Cache clear error")
            text = f"🗑 <b>Clear media cache</b>\n\n❌ Error: {esc(str(exc))}"
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin:home")]]),
        )
        return

    if data == "admin:changelog":
        await query.edit_message_text(
            format_changelog_for_telegram(),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin:home")]]),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:api":
        await query.edit_message_text(
            _api_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=api_menu_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:api_id":
        context.user_data[AWAIT_API_ID] = True
        await query.message.reply_text(
            "📝 <b>Send your API ID</b>\n\n"
            f"Get it from <a href=\"{MY_TELEGRAM_ORG}\">my.telegram.org</a> → API development tools.\n"
            "Reply with numbers only, or /cancel.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data == "admin:api_hash":
        context.user_data[AWAIT_API_HASH] = True
        await query.message.reply_text(
            "🔐 <b>Send your API Hash</b>\n\n"
            f"From <a href=\"{MY_TELEGRAM_ORG}\">my.telegram.org</a> (same page as API ID).\n"
            "Reply with the hash string, or /cancel.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if data == "admin:api_clear":
        env_store.remove_env_key("TELEGRAM_API_ID")
        env_store.remove_env_key("TELEGRAM_API_HASH")
        reload_settings()
        await reset_telethon_client()
        await query.edit_message_text(
            _api_text() + "\n\n✅ Credentials removed. Upload limit is now <b>50 MB</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=api_menu_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:speedtest":
        await query.edit_message_text(
            _speedtest_prompt_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("▶️ Run test", callback_data="admin:speedtest_run")],
                    [InlineKeyboardButton("« Back", callback_data="admin:home")],
                ]
            ),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:speedtest_run":
        status_msg = await query.message.reply_text(
            "⚡ <b>Speed test</b>\n\n⏳ Preparing…",
            parse_mode=ParseMode.HTML,
        )
        try:
            report = await run_speed_test(query.message, status_msg=status_msg)
            await status_msg.edit_text(
                report,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Back", callback_data="admin:home")]]
                ),
            )
        except Exception as exc:
            logger.exception("Speed test error")
            await status_msg.edit_text(
                f"❌ Speed test failed: {esc(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« Back", callback_data="admin:home")]]
                ),
            )
        return

    if data == "admin:update":
        await query.edit_message_text(
            "🔎 <b>Checking GitHub for updates…</b>",
            parse_mode=ParseMode.HTML,
        )
        try:
            result = await asyncio.to_thread(fetch_update_check)
            text = format_update_panel_message(result)
        except Exception as exc:
            logger.exception("Update check on Update bot failed")
            text = (
                "🔄 <b>Update bot</b>\n\n"
                f"⚠️ Check failed: {esc(str(exc))}\n\n"
                "You can still run an update."
            )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=update_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:update_confirm":
        chat_id = query.message.chat_id
        user_id = query.from_user.id if query.from_user else 0
        await start_bot_update(
            context.application,
            chat_id,
            user_id,
            status_message_id=query.message.message_id,
            notify=False,
        )
        return


async def admin_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle admin replies when setting API credentials. Returns True if handled."""
    if not _require_admin_dm(update):
        return False

    if not (context.user_data.get(AWAIT_API_ID) or context.user_data.get(AWAIT_API_HASH)):
        return False

    if context.user_data.get(AWAIT_API_ID):
        text = (update.message.text or "").strip()
        if not text.isdigit():
            await update.message.reply_text("API ID must be numbers only. Try again or /cancel.")
            return True
        env_store.update_env_value("TELEGRAM_API_ID", text)
        context.user_data.pop(AWAIT_API_ID, None)
        reload_settings()
        await reset_telethon_client()
        await update.message.reply_text(
            f"✅ API ID saved: <code>{esc(text)}</code>\n\n{_api_status_line()}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return True

    if context.user_data.get(AWAIT_API_HASH):
        text = (update.message.text or "").strip()
        if len(text) < 16:
            await update.message.reply_text("That hash looks too short. Try again or /cancel.")
            return True
        env_store.update_env_value("TELEGRAM_API_HASH", text)
        context.user_data.pop(AWAIT_API_HASH, None)
        reload_settings()
        await reset_telethon_client()
        await update.message.reply_text(
            f"✅ API Hash saved: <code>{env_store.mask_secret(text)}</code>\n\n{_api_status_line()}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )
        return True

    return False


async def cancel_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin_dm(update):
        return
    context.user_data.pop(AWAIT_API_ID, None)
    context.user_data.pop(AWAIT_API_HASH, None)
    await update.message.reply_text("Cancelled.", reply_markup=admin_menu_keyboard())


def admin_keyboard_for_start() -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⚙️ Admin panel", callback_data="admin:home")]]
    )


def _panel_header() -> str:
    mode = "🚀 <b>2 GB mode ON</b>" if large_upload_enabled() else "📦 <b>50 MB mode</b>"
    return (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"{format_version_label()} · {mode} · Upload limit: <b>{upload_limit_label()}</b>\n\n"
        "Choose an option below:"
    )


def _stats_text() -> str:
    s = stats.get_stats_summary()
    return (
        "📊 <b>Statistics</b>\n\n"
        f"Total downloads: <b>{s['total_downloads']}</b>\n"
        f"Unique users: <b>{s['unique_users']}</b>\n"
        f"Today: <b>{s['downloads_today']}</b>\n"
        f"Data sent: <b>{format_size(s['bytes_total'])}</b>"
    )


def _logs_text() -> str:
    rows = stats.get_recent_logs(12)
    if not rows:
        return "👥 <b>Recent downloads</b>\n\n<i>No downloads yet.</i>"

    lines = ["👥 <b>Recent downloads</b>\n"]
    for row in rows:
        ts = row["created_at"][:16].replace("T", " ")
        user = f"@{row['username']}" if row["username"] else f"id:{row['user_id']}"
        platform = row["platform"] or "?"
        size = format_size(row["file_size"]) if row["file_size"] else "?"
        lines.append(f"• {ts} · {esc(user)} · {platform} · {size}")
    return "\n".join(lines)


def _failures_text() -> str:
    """Unsupported sites + extract failures — what to add or fix."""
    hosts = stats.get_failure_host_counts(10)
    rows = stats.get_recent_failures(15)
    lines = [
        "⚠️ <b>Failures &amp; requests</b>",
        "",
        "<i>Unsupported links users tried, and supported sites that broke.</i>",
        "",
    ]
    if hosts:
        lines.append("<b>Top hosts</b>")
        for row in hosts:
            kind = "➕ add?" if row["kind"] == "unsupported" else "🔧 broken?"
            host = row["host"] or "?"
            lines.append(f"• <code>{esc(host)}</code> · {row['n']}× · {kind}")
        lines.append("")
    if not rows:
        lines.append("<i>No failures logged yet.</i>")
        return "\n".join(lines)

    lines.append("<b>Recent</b>")
    for row in rows:
        ts = (row["created_at"] or "")[:16].replace("T", " ")
        kind = "unsupported" if row["kind"] == "unsupported" else "failed"
        host = row["host"] or row["platform"] or "?"
        err = (row["error"] or "").replace("\n", " ")
        if len(err) > 80:
            err = err[:79] + "…"
        lines.append(f"• {ts} · <b>{kind}</b> · <code>{esc(host)}</code>")
        if err and kind == "failed":
            lines.append(f"  <i>{esc(err)}</i>")
        url = row["url"] or ""
        if url:
            short = url if len(url) <= 60 else url[:59] + "…"
            lines.append(f"  <code>{esc(short)}</code>")
    return "\n".join(lines)


def _disk_text() -> str:
    d = stats.get_disk_info()
    used_pct = d["disk_used"] / d["disk_total"] * 100 if d["disk_total"] else 0
    return (
        "💾 <b>Disk &amp; storage</b>\n\n"
        f"<b>VPS disk</b>\n"
        f"Used: {format_size(d['disk_used'])} / {format_size(d['disk_total'])} ({used_pct:.0f}%)\n"
        f"Free: {format_size(d['disk_free'])}\n\n"
        f"<b>downloads/ folder</b>\n"
        f"{format_size(d['downloads_bytes'])} (temp files, auto-cleaned)"
    )


def _cache_prompt_text() -> str:
    try:
        n = media_cache.cache_count()
    except Exception:
        n = 0
    return (
        "🗑 <b>Clear media cache</b>\n\n"
        f"Cached links: <b>{n}</b>\n\n"
        "This deletes saved Telegram shortcuts so the next request "
        "downloads fresh media (useful after updates).\n\n"
        "Stats and temp files are not affected."
    )


def _api_text() -> str:
    return (
        "🔑 <b>2 GB upload — Telegram Core API</b>\n\n"
        "Standard bots are limited to <b>50 MB</b>. With API ID + API Hash from "
        f'<a href="{MY_TELEGRAM_ORG}">my.telegram.org</a>, the bot uses '
        "<b>Telegram MTProto</b> to upload files up to <b>2 GB</b>.\n\n"
        f"<b>Status:</b> {_api_status_line()}\n\n"
        f"API ID: <code>{esc(TELEGRAM_API_ID) if TELEGRAM_API_ID else '— not set —'}</code>\n"
        f"API Hash: <code>{env_store.mask_secret(TELEGRAM_API_HASH) if TELEGRAM_API_HASH else '— not set —'}</code>\n\n"
        "<i>Both values are required. Get them from my.telegram.org → "
        "API development tools → Create application.</i>"
    )


def _api_status_line() -> str:
    if large_upload_enabled():
        return "✅ Configured — uploads up to <b>2 GB</b>"
    if TELEGRAM_API_ID or TELEGRAM_API_HASH:
        return "⚠️ Incomplete — set both API ID and API Hash"
    return "❌ Not configured — max upload <b>50 MB</b>"


def _update_prompt_text() -> str:
    return (
        "🔄 <b>Update bot</b>\n\n"
        "This will:\n"
        "• Sync the latest code from GitHub\n"
        "• Update packages\n"
        "• Restart the bot\n\n"
        "The bot will go offline briefly, then confirm here when it’s back."
    )


def _speedtest_prompt_text() -> str:
    return (
        "⚡ <b>Download / upload speed test</b>\n\n"
        "Downloads a <b>5 MB</b> test file from Cloudflare, then uploads it "
        "back to this chat via Bot API (files ≤50 MB) or Telethon when larger "
        "and 2 GB mode is on.\n\n"
        "Use this to check VPS ↔ Telegram throughput."
    )


async def start_bot_update(
    application,
    chat_id: int,
    user_id: int,
    *,
    status_message_id: int | None = None,
    notify: bool = True,
) -> None:
    if notify and not status_message_id:
        msg = await application.bot.send_message(
            chat_id,
            "🔄 <b>Bot update</b>\n\n⏳ Starting…",
            parse_mode=ParseMode.HTML,
        )
        status_message_id = msg.message_id

    schedule_update_notification(chat_id, user_id, status_message_id=status_message_id)
    asyncio.create_task(run_update_script(application, chat_id, status_message_id))
