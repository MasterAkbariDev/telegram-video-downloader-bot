"""Admin settings panel — stats, logs, 2 GB API credentials."""

from __future__ import annotations

import asyncio
import logging
import queue as _queue

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import config as cfg
from bot import env_store, media_cache, stats
from bot.changelog import format_changelog_for_telegram
from bot.config import is_admin, large_upload_enabled, reload_settings
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
AWAIT_IG_COOKIES = "admin_await_ig_cookies"
AWAIT_IG_USERNAME = "admin_await_ig_username"
AWAIT_IG_PASSWORD = "admin_await_ig_password"
AWAIT_IG_PROXY = "admin_await_ig_proxy"
AWAIT_IG_TOTP = "admin_await_ig_totp"
AWAIT_IG_SIGNUP_USERNAME = "admin_await_ig_signup_username"
AWAIT_IG_SIGNUP_PASSWORD = "admin_await_ig_signup_password"
AWAIT_IG_SIGNUP_EMAIL = "admin_await_ig_signup_email"
AWAIT_IG_SIGNUP_FULLNAME = "admin_await_ig_signup_fullname"
IG_SIGNUP_DATA = "admin_ig_signup_data"

# Every "waiting for a text/document reply" flag — keep this in sync when
# adding a new one, since /cancel and cancel_admin_input rely on it to know
# whether there's anything to cancel.
_ALL_AWAIT_KEYS = (
    AWAIT_API_ID,
    AWAIT_API_HASH,
    AWAIT_IG_COOKIES,
    AWAIT_IG_USERNAME,
    AWAIT_IG_PASSWORD,
    AWAIT_IG_PROXY,
    AWAIT_IG_TOTP,
    AWAIT_IG_SIGNUP_USERNAME,
    AWAIT_IG_SIGNUP_PASSWORD,
    AWAIT_IG_SIGNUP_EMAIL,
    AWAIT_IG_SIGNUP_FULLNAME,
)

MY_TELEGRAM_ORG = "https://my.telegram.org/apps"

# admin user_id -> Queue, while that admin has a live interactive Instagram
# login/signup waiting on a verification code they need to send in chat.
_pending_ig_code_queues: dict[int, "_queue.Queue[str]"] = {}


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
            [InlineKeyboardButton("📸 Instagram cookies", callback_data="admin:ig_cookies")],
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


def ig_cookies_menu_keyboard() -> InlineKeyboardMarkup:
    from bot.instagram_auth import instagram_cookies_status

    status = instagram_cookies_status()
    rows: list[list[InlineKeyboardButton]] = []

    if status["auto_login_configured"]:
        rows.append(
            [
                InlineKeyboardButton("🔐 Login now", callback_data="admin:ig_login"),
                InlineKeyboardButton("🚪 Logout", callback_data="admin:ig_logout"),
            ]
        )
    rows.append([InlineKeyboardButton("➕ Create account", callback_data="admin:ig_signup")])
    rows.append(
        [
            InlineKeyboardButton("✏️ Username", callback_data="admin:ig_username"),
            InlineKeyboardButton("✏️ Password", callback_data="admin:ig_password"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🌐 Proxy", callback_data="admin:ig_proxy"),
            InlineKeyboardButton("🔑 TOTP secret", callback_data="admin:ig_totp"),
        ]
    )
    rows.append([InlineKeyboardButton("⬆️ Upload cookies.txt", callback_data="admin:ig_cookies_upload")])
    if status["exists"]:
        rows.append(
            [InlineKeyboardButton("🗑 Remove cookies", callback_data="admin:ig_cookies_clear")]
        )
    rows.append([InlineKeyboardButton("« Back", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)


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
    # Pick up any .env edits made outside the bot (SSH/manual edit) without
    # requiring a restart — otherwise the panel can show stale state, e.g.
    # "Login now" missing after adding INSTAGRAM_USERNAME/PASSWORD by hand.
    reload_settings()
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

    # Pick up any .env edits made outside the bot before rendering anything.
    if data != "admin:close":
        reload_settings()

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

    if data == "admin:ig_cookies":
        await query.edit_message_text(
            _ig_cookies_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=ig_cookies_menu_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:ig_cookies_upload":
        context.user_data[AWAIT_IG_COOKIES] = True
        await query.message.reply_text(
            "⬆️ <b>Send your cookies.txt file</b>\n\n"
            "Export it from a browser logged into Instagram (Netscape format), "
            "then send it here as a document. Or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin:ig_cookies_clear":
        from bot.instagram_auth import INSTAGRAM_COOKIES_PATH

        INSTAGRAM_COOKIES_PATH.unlink(missing_ok=True)
        await query.edit_message_text(
            _ig_cookies_text() + "\n\n✅ Cookies removed.",
            parse_mode=ParseMode.HTML,
            reply_markup=ig_cookies_menu_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "admin:ig_login":
        from bot.instagram_auth import instagram_credentials_configured, interactive_login

        if not instagram_credentials_configured():
            await query.message.reply_text(
                "❌ Set an Instagram username and password first (✏️ Username / ✏️ Password).",
            )
            return
        admin_id = query.from_user.id
        status_msg = await query.message.reply_text("🔐 <b>Logging in…</b>", parse_mode=ParseMode.HTML)
        loop = asyncio.get_running_loop()
        provider = _make_ig_code_provider(admin_id, context.bot, loop)
        try:
            result = await asyncio.to_thread(interactive_login, provider)
            await status_msg.edit_text(
                f"✅ Logged in as <code>{esc(result['username'])}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        except Exception as exc:
            logger.warning("Interactive Instagram login failed: %s", exc)
            await status_msg.edit_text(
                f"❌ Login failed: {esc(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        return

    if data == "admin:ig_logout":
        from bot.instagram_auth import logout_instagram

        status_msg = await query.message.reply_text("🚪 <b>Logging out…</b>", parse_mode=ParseMode.HTML)
        try:
            await asyncio.to_thread(logout_instagram)
            await status_msg.edit_text(
                "✅ Logged out — local session and cookies cleared.",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        except Exception as exc:
            logger.warning("Instagram logout failed: %s", exc)
            await status_msg.edit_text(
                f"❌ Logout error: {esc(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        return

    if data == "admin:ig_username":
        context.user_data[AWAIT_IG_USERNAME] = True
        await query.message.reply_text(
            "✏️ <b>Send the Instagram username</b>\n\nOr /cancel.", parse_mode=ParseMode.HTML
        )
        return

    if data == "admin:ig_password":
        context.user_data[AWAIT_IG_PASSWORD] = True
        await query.message.reply_text(
            "✏️ <b>Send the Instagram password</b>\n\nOr /cancel.", parse_mode=ParseMode.HTML
        )
        return

    if data == "admin:ig_proxy":
        context.user_data[AWAIT_IG_PROXY] = True
        await query.message.reply_text(
            "🌐 <b>Send a proxy URL</b> for Instagram login (residential/mobile "
            "recommended), e.g. <code>http://user:pass@host:port</code>.\n\n"
            "Send <code>clear</code> to remove it, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin:ig_totp":
        context.user_data[AWAIT_IG_TOTP] = True
        await query.message.reply_text(
            "🔑 <b>Send the TOTP seed</b> for automated 2FA (Instagram: Settings "
            "→ Two-factor authentication → Authentication app → \"Can't scan the "
            "QR code?\").\n\nSend <code>clear</code> to remove it, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin:ig_signup":
        context.user_data[IG_SIGNUP_DATA] = {}
        context.user_data[AWAIT_IG_SIGNUP_USERNAME] = True
        await query.message.reply_text(
            "➕ <b>Create an Instagram account</b>\n\n"
            "This uses email verification — have a real inbox ready; I'll ask "
            "you to paste the code here when Instagram sends it. Success isn't "
            "guaranteed — Instagram may still require a phone number or a "
            "captcha this can't solve, especially from a server IP.\n\n"
            "Send the desired <b>username</b>, or /cancel.",
            parse_mode=ParseMode.HTML,
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


def _make_ig_code_provider(admin_id: int, bot, loop: asyncio.AbstractEventLoop):
    """Bridge instagrapi's synchronous challenge_code_handler (called from a
    worker thread) to a Telegram prompt+reply, so an interactive login/signup
    can ask the admin for a verification code mid-flow."""

    def provider(username: str, choice=None) -> str:
        q: "_queue.Queue[str]" = _queue.Queue()
        _pending_ig_code_queues[admin_id] = q
        short_labels = {1: "email", 0: "SMS"}
        if choice in short_labels:
            text = (
                f"📩 <b>Instagram sent a verification code</b> ({short_labels[choice]}) "
                f"for <code>{esc(username)}</code>.\n\nReply with the code, or /cancel."
            )
        elif isinstance(choice, str) and choice.strip():
            # A fuller, human-readable prompt (e.g. an age/birthdate
            # confirmation) — show it as-is rather than wrapping it oddly.
            text = (
                f"📩 <b>Instagram needs one more thing for</b> <code>{esc(username)}</code>:\n"
                f"{esc(choice)}\n\nReply here, or /cancel."
            )
        else:
            text = (
                f"📩 <b>Instagram sent a verification code</b> for "
                f"<code>{esc(username)}</code>.\n\nReply with the code, or /cancel."
            )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                bot.send_message(admin_id, text, parse_mode=ParseMode.HTML), loop
            )
            fut.result(timeout=15)
        except Exception as exc:
            logger.warning("Could not prompt admin for Instagram code: %s", exc)
        try:
            return q.get(timeout=300)
        except _queue.Empty:
            return ""
        finally:
            _pending_ig_code_queues.pop(admin_id, None)

    return provider


async def admin_ig_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Relay a text reply into a pending interactive-login/signup code request."""
    if not _require_admin_dm(update):
        return False
    user = update.effective_user
    if not user:
        return False
    q = _pending_ig_code_queues.get(user.id)
    if q is None:
        return False
    text = (update.message.text or "").strip()
    q.put(text)
    await update.message.reply_text("⏳ Got it — resolving…")
    return True


async def admin_settings_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle admin replies when setting credentials. Returns True if handled."""
    if not _require_admin_dm(update):
        return False

    if await admin_ig_code_input(update, context):
        return True

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

    if context.user_data.get(AWAIT_IG_USERNAME):
        text = (update.message.text or "").strip().lstrip("@")
        if not text:
            await update.message.reply_text("Username can't be empty. Try again or /cancel.")
            return True
        env_store.update_env_value("INSTAGRAM_USERNAME", text)
        context.user_data.pop(AWAIT_IG_USERNAME, None)
        reload_settings()
        await update.message.reply_text(
            f"✅ Instagram username saved: <code>{esc(text)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ig_cookies_menu_keyboard(),
        )
        return True

    if context.user_data.get(AWAIT_IG_PASSWORD):
        text = update.message.text or ""
        if not text.strip():
            await update.message.reply_text("Password can't be empty. Try again or /cancel.")
            return True
        env_store.update_env_value("INSTAGRAM_PASSWORD", text.strip())
        context.user_data.pop(AWAIT_IG_PASSWORD, None)
        reload_settings()
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "✅ Instagram password saved.",
            parse_mode=ParseMode.HTML,
            reply_markup=ig_cookies_menu_keyboard(),
        )
        return True

    if context.user_data.get(AWAIT_IG_PROXY):
        text = (update.message.text or "").strip()
        context.user_data.pop(AWAIT_IG_PROXY, None)
        if text.lower() in {"clear", "none", "remove", "-"}:
            env_store.remove_env_key("INSTAGRAM_PROXY")
            reload_settings()
            await update.message.reply_text("✅ Instagram proxy cleared.", reply_markup=ig_cookies_menu_keyboard())
            return True
        env_store.update_env_value("INSTAGRAM_PROXY", text)
        reload_settings()
        await update.message.reply_text(
            f"✅ Instagram proxy saved: <code>{esc(text)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ig_cookies_menu_keyboard(),
        )
        return True

    if context.user_data.get(AWAIT_IG_TOTP):
        text = (update.message.text or "").strip().replace(" ", "")
        context.user_data.pop(AWAIT_IG_TOTP, None)
        if text.lower() in {"clear", "none", "remove", "-"}:
            env_store.remove_env_key("INSTAGRAM_TOTP_SECRET")
            reload_settings()
            await update.message.reply_text("✅ TOTP secret cleared.", reply_markup=ig_cookies_menu_keyboard())
            return True
        env_store.update_env_value("INSTAGRAM_TOTP_SECRET", text)
        reload_settings()
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "✅ TOTP secret saved.", reply_markup=ig_cookies_menu_keyboard()
        )
        return True

    if await _admin_ig_signup_input(update, context):
        return True

    return False


async def _admin_ig_signup_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Multi-step wizard: username → password → email → full name → create."""
    data = context.user_data.get(IG_SIGNUP_DATA)

    if context.user_data.get(AWAIT_IG_SIGNUP_USERNAME):
        text = (update.message.text or "").strip().lstrip("@")
        if not text:
            await update.message.reply_text("Username can't be empty. Try again or /cancel.")
            return True
        data["username"] = text
        context.user_data.pop(AWAIT_IG_SIGNUP_USERNAME, None)
        context.user_data[AWAIT_IG_SIGNUP_PASSWORD] = True
        await update.message.reply_text("Now send the <b>password</b> for this account.", parse_mode=ParseMode.HTML)
        return True

    if context.user_data.get(AWAIT_IG_SIGNUP_PASSWORD):
        text = update.message.text or ""
        if len(text.strip()) < 6:
            await update.message.reply_text("Password looks too short. Try again or /cancel.")
            return True
        data["password"] = text.strip()
        context.user_data.pop(AWAIT_IG_SIGNUP_PASSWORD, None)
        context.user_data[AWAIT_IG_SIGNUP_EMAIL] = True
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "Now send the <b>email address</b> to verify with (you'll need to "
            "check its inbox for a code in a moment).",
            parse_mode=ParseMode.HTML,
        )
        return True

    if context.user_data.get(AWAIT_IG_SIGNUP_EMAIL):
        text = (update.message.text or "").strip()
        if "@" not in text:
            await update.message.reply_text("That doesn't look like an email. Try again or /cancel.")
            return True
        data["email"] = text
        context.user_data.pop(AWAIT_IG_SIGNUP_EMAIL, None)
        context.user_data[AWAIT_IG_SIGNUP_FULLNAME] = True
        await update.message.reply_text(
            "Optional: send a <b>full name</b> to display, or send <code>skip</code>.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if context.user_data.get(AWAIT_IG_SIGNUP_FULLNAME):
        text = (update.message.text or "").strip()
        data["full_name"] = "" if text.lower() == "skip" else text
        context.user_data.pop(AWAIT_IG_SIGNUP_FULLNAME, None)
        context.user_data.pop(IG_SIGNUP_DATA, None)

        from bot.instagram_auth import create_instagram_account

        admin_id = update.effective_user.id
        status_msg = await update.message.reply_text(
            "➕ <b>Creating account…</b> Instagram will email a code shortly.",
            parse_mode=ParseMode.HTML,
        )
        loop = asyncio.get_running_loop()
        provider = _make_ig_code_provider(admin_id, context.bot, loop)
        try:
            result = await asyncio.to_thread(
                create_instagram_account,
                code_provider=provider,
                username=data["username"],
                password=data["password"],
                email=data["email"],
                full_name=data.get("full_name", ""),
            )
            env_store.update_env_value("INSTAGRAM_USERNAME", result["username"])
            env_store.update_env_value("INSTAGRAM_PASSWORD", data["password"])
            reload_settings()
            await status_msg.edit_text(
                f"✅ Account created and logged in: <code>{esc(result['username'])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        except Exception as exc:
            logger.warning("Instagram signup failed: %s", exc)
            await status_msg.edit_text(
                f"❌ Account creation failed: {esc(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=ig_cookies_menu_keyboard(),
            )
        return True

    return False


async def admin_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle an admin uploading cookies.txt. Returns True if handled."""
    if not _require_admin_dm(update):
        return False
    if not context.user_data.get(AWAIT_IG_COOKIES):
        return False

    document = update.message.document
    if not document:
        return False

    context.user_data.pop(AWAIT_IG_COOKIES, None)

    if document.file_size and document.file_size > 2 * 1024 * 1024:
        await update.message.reply_text(
            "❌ That file is too large to be a cookies.txt export (>2 MB).",
            reply_markup=admin_menu_keyboard(),
        )
        return True

    try:
        tg_file = await document.get_file()
        raw = await tg_file.download_as_bytearray()
    except Exception as exc:
        logger.warning("Instagram cookies download failed: %s", exc)
        await update.message.reply_text(
            f"❌ Could not download that file: {esc(str(exc))}",
            reply_markup=admin_menu_keyboard(),
        )
        return True

    from bot.instagram_auth import save_uploaded_instagram_cookies

    try:
        save_uploaded_instagram_cookies(bytes(raw))
    except ValueError as exc:
        await update.message.reply_text(
            f"❌ {esc(str(exc))}\n\nMake sure you exported a Netscape-format "
            "cookies.txt while logged into Instagram, then try again.",
            reply_markup=ig_cookies_menu_keyboard(),
        )
        return True

    await update.message.reply_text(
        "✅ Instagram cookies saved and validated.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )
    return True


def has_pending_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if /cancel would actually have something to do for this admin."""
    if any(context.user_data.get(key) for key in _ALL_AWAIT_KEYS):
        return True
    user = update.effective_user
    return bool(user and user.id in _pending_ig_code_queues)


async def cancel_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _require_admin_dm(update):
        return
    for key in _ALL_AWAIT_KEYS:
        context.user_data.pop(key, None)
    context.user_data.pop(IG_SIGNUP_DATA, None)

    user = update.effective_user
    if user:
        q = _pending_ig_code_queues.get(user.id)
        if q is not None:
            q.put("")  # unblock any interactive login/signup waiting on a code

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


def _ig_cookies_text() -> str:
    from bot.instagram_auth import instagram_cookies_status

    status = instagram_cookies_status()
    if not status["exists"]:
        state = "❌ Not set — public posts only; private/checkpoint-gated posts will fail"
    elif not status["valid"]:
        state = "⚠️ File exists but looks invalid/expired — upload a fresh export"
    else:
        age = status["age_days"]
        age_txt = f"{age:.1f} days old" if age is not None else "age unknown"
        state = f"✅ Active ({age_txt})"

    if status["auto_login_configured"]:
        bits = [f"✅ <code>{esc(status['username'] or '')}</code>"]
        bits.append("device session saved" if status["session_saved"] else "no saved session yet")
        bits.append("proxy set" if status["proxy_configured"] else "⚠️ no proxy — higher risk of rejection")
        bits.append("TOTP set" if status["totp_configured"] else "no TOTP (2FA accounts need this)")
        auto_login = " · ".join(bits)
        cooldown = status["cooldown_remaining_sec"]
        if cooldown > 0:
            auto_login += f"\n⏳ Cooling down after a failed attempt — {cooldown / 60:.0f} min left"
    else:
        auto_login = "❌ not configured"

    from bot.hikerapi import hikerapi_configured

    hiker_line = (
        "✅ configured — used as a last resort for login-walled posts"
        if hikerapi_configured()
        else "❌ not set — recommended over auto-login for reliability at scale, see README"
    )

    return (
        "📸 <b>Instagram</b>\n\n"
        f"<b>Cookies:</b> {state}\n"
        f"<b>Auto-login:</b> {auto_login}\n"
        f"<b>HikerAPI (HIKERAPI_KEY):</b> {hiker_line}\n\n"
        "Auto-login uses Instagram's mobile app login flow with a persisted "
        "device fingerprint, and supports automated 2FA via TOTP. It works "
        "best — and is far less likely to be rejected — with a <b>proxy</b> "
        "set to a residential/mobile IP; server/datacenter IPs are what "
        "Instagram's risk system flags hardest, sometimes rejecting even "
        "correct credentials.\n\n"
        "<b>🔐 Login now</b> resolves checkpoints interactively — if "
        "Instagram sends a verification code, I'll ask you for it here.\n\n"
        "Alternatively (or if you'd rather not store a password at all), "
        "upload a <b>cookies.txt</b> exported from a real, logged-in browser "
        "session (e.g. with the “Get cookies.txt LOCALLY” extension) — it "
        "isn't refreshed automatically, so re-upload when it expires.\n\n"
        "Either way: the account must <b>follow</b> a private account for its "
        "posts to be downloadable — no login or cookie trick bypasses that."
    )


def _api_text() -> str:
    return (
        "🔑 <b>2 GB upload — Telegram Core API</b>\n\n"
        "Standard bots are limited to <b>50 MB</b>. With API ID + API Hash from "
        f'<a href="{MY_TELEGRAM_ORG}">my.telegram.org</a>, the bot uses '
        "<b>Telegram MTProto</b> to upload files up to <b>2 GB</b>.\n\n"
        f"<b>Status:</b> {_api_status_line()}\n\n"
        f"API ID: <code>{esc(cfg.TELEGRAM_API_ID) if cfg.TELEGRAM_API_ID else '— not set —'}</code>\n"
        f"API Hash: <code>{env_store.mask_secret(cfg.TELEGRAM_API_HASH) if cfg.TELEGRAM_API_HASH else '— not set —'}</code>\n\n"
        "<i>Both values are required. Get them from my.telegram.org → "
        "API development tools → Create application.</i>"
    )


def _api_status_line() -> str:
    if large_upload_enabled():
        return "✅ Configured — uploads up to <b>2 GB</b>"
    if cfg.TELEGRAM_API_ID or cfg.TELEGRAM_API_HASH:
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
