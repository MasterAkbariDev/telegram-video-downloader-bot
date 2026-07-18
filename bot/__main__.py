"""Entry point: python -m bot"""

import asyncio
import logging
import sys

from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from bot.admin import (
    admin_callback,
    admin_command,
    update_command,
)
from bot.config import BOT_TOKEN, REQUEST_TIMEOUT, TELEGRAM_PROXY
from bot.handlers import (
    about_command,
    cancel_command,
    chosen_inline_result_handler,
    handle_message,
    help_command,
    inline_query_handler,
    quality_callback,
    start_command,
)
from bot import stats
from bot.updater import notify_pending_update
from bot.update_check import notify_admins_if_update_available, update_check_loop
from bot.version import format_version_label
from bot import media_cache

logger = logging.getLogger(__name__)


async def _post_init(application) -> None:
    media_cache.init_db()
    await notify_pending_update(application)
    # One-time alert per new GitHub version (admins only)
    await notify_admins_if_update_available(application)
    # Keep checking in the background; still only alerts once per version
    asyncio.create_task(update_check_loop(application))


def _build_application() -> Application:
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(REQUEST_TIMEOUT)
        .read_timeout(REQUEST_TIMEOUT)
        .write_timeout(REQUEST_TIMEOUT)
        .pool_timeout(REQUEST_TIMEOUT)
        .get_updates_connect_timeout(REQUEST_TIMEOUT)
        .get_updates_read_timeout(REQUEST_TIMEOUT + 30)
        .get_updates_write_timeout(REQUEST_TIMEOUT)
        .get_updates_pool_timeout(REQUEST_TIMEOUT)
    )

    if TELEGRAM_PROXY:
        logger.info("Using proxy: %s", TELEGRAM_PROXY.split("@")[-1])
        builder = builder.proxy_url(TELEGRAM_PROXY)

    builder = builder.post_init(_post_init)
    return builder.build()


def _print_connection_help() -> None:
    print("\nCannot reach Telegram API (connection timed out).\n")
    print("On your server, test connectivity:")
    print("  curl -s --max-time 15 https://api.telegram.org/bot<YOUR_TOKEN>/getMe\n")
    print("If that fails, Telegram may be blocked on your server/network.")
    print("Fix options:")
    print("  1. Use a VPS in a region where Telegram is not blocked")
    print("  2. Add a proxy to .env:")
    print("       TELEGRAM_PROXY=socks5://127.0.0.1:1080")
    print("     then restart: ./run.sh")
    print("  3. Check outbound firewall allows HTTPS to api.telegram.org\n")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        print("ERROR: BOT_TOKEN is not set. Run ./setup.sh first.")
        sys.exit(1)

    app = _build_application()
    stats.init_db()
    logger.info("Starting %s", format_version_label())

    # Admin commands & callbacks
    app.add_handler(CommandHandler("admin", admin_command), group=0)
    app.add_handler(CommandHandler("update", update_command, filters=filters.ChatType.PRIVATE), group=0)
    app.add_handler(CommandHandler("cancel", cancel_command), group=0)
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:",
            block=True,
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            quality_callback,
            pattern=r"^q:",
            block=True,
        ),
        group=0,
    )

    # User commands & messages
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    print(f"Bot {format_version_label()} is running. Press Ctrl+C to stop.")
    if TELEGRAM_PROXY:
        print(f"Proxy enabled: {TELEGRAM_PROXY.split('@')[-1]}")

    try:
        app.run_polling(
            allowed_updates=["message", "inline_query", "chosen_inline_result", "callback_query"],
            bootstrap_retries=-1,
            drop_pending_updates=True,
        )
    except (TimedOut, NetworkError):
        _print_connection_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
