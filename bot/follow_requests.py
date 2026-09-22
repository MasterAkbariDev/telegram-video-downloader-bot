"""Send a follow request to a private Instagram account on behalf of any bot
user, and tell them once it's actually accepted or rejected.

Instagram gives no webhook for a follow-request decision, so this needs a
background poll loop — follow_request_poll_loop() mirrors the existing
bot/update_check.py:update_check_loop() pattern (a plain asyncio loop
scheduled once via asyncio.create_task in bot/__main__.py's _post_init, no
new dependency).

Mass-following is one of the most heavily flagged Instagram behaviors, so a
public "request any private account" entry point needs its own cooldown —
separate from (and in addition to) the existing per-account login cooldowns
in bot/instagram_auth.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from bot import config as cfg
from bot import stats
from bot.instagram_auth import _build_client, _safe_err, pick_enabled_account

logger = logging.getLogger(__name__)

_REQUESTER_COOLDOWN_SEC = 10 * 60  # one follow request per user per 10 minutes
_last_request_at: dict[int, float] = {}
_POLL_INTERVAL_SEC = 180  # re-check pending requests every 3 minutes

# Instagram usernames: letters, numbers, periods, underscores, 1-30 chars.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,30}$")
_NON_PROFILE_PATHS = {
    "p", "reel", "reels", "stories", "explore", "accounts", "tv", "direct", "web", "graphql", "api",
}

# user_id -> expiry (monotonic), while the "🔒 Request private account"
# button is waiting on that user's next text message.
_TARGET_INPUT_TTL_SEC = 300.0
_awaiting_target: dict[int, float] = {}


def mark_awaiting_target(user_id: int) -> None:
    _awaiting_target[user_id] = time.monotonic() + _TARGET_INPUT_TTL_SEC


def is_awaiting_target(user_id: int) -> bool:
    expiry = _awaiting_target.get(user_id)
    if expiry is None:
        return False
    if time.monotonic() > expiry:
        _awaiting_target.pop(user_id, None)
        return False
    return True


def clear_awaiting_target(user_id: int) -> None:
    _awaiting_target.pop(user_id, None)


def parse_target_input(text: str) -> tuple[str | None, str | None]:
    """Accepts a username, a numeric user id, or a profile link
    (instagram.com/<username>). Returns (target, None) on success, or
    (None, error_message) if it doesn't even look like a valid target —
    this is a shape check only; whether the account actually exists is
    only known once we try to resolve it against Instagram.
    """
    text = (text or "").strip()
    if not text:
        return None, "Send a username, numeric ID, or profile link."

    if "instagram.com" in text.lower():
        path = re.sub(r"^https?://", "", text, flags=re.I)
        path = path.split("?", 1)[0].split("#", 1)[0]
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return None, "That link doesn't look like a profile link (no username in it)."
        username = parts[1]
        if username.lower() in _NON_PROFILE_PATHS:
            return None, "That's a post/reel link, not a profile link — send the account's profile link instead."
        text = username

    text = text.strip().lstrip("@")
    if text.isdigit():
        return text, None
    if not _USERNAME_RE.match(text):
        return None, "That doesn't look like a valid Instagram username."
    return text, None


def requester_cooldown_remaining(user_id: int) -> float:
    last = _last_request_at.get(user_id, 0.0)
    return max(0.0, _REQUESTER_COOLDOWN_SEC - (time.time() - last))


def _resolve_target(cl, target: str) -> tuple[str, str]:
    """(user_id, username) for a target given as a username or numeric id."""
    target = target.strip().lstrip("@")
    if target.isdigit():
        info = cl.user_info(target)
        return target, info.username
    user_id = cl.user_id_from_username(target)
    return user_id, target


def send_follow_request(requester_user_id: int, requester_chat_id: int, target: str) -> dict:
    """Send a follow request from a random enabled account.

    Returns {"status": "accepted"|"pending"|"error", "account": str,
    "target_username": str, "detail": str}. "accepted" here can mean either
    the target wasn't actually private (follow succeeds instantly) or
    Instagram auto-approved it; "pending" means it genuinely has to wait on
    the target — the poll loop will notify the requester's chat once it
    resolves.
    """
    wait_left = requester_cooldown_remaining(requester_user_id)
    if wait_left > 0:
        return {
            "status": "error",
            "detail": f"Please wait {wait_left / 60:.0f} more minute(s) before requesting again.",
        }

    account = pick_enabled_account()
    if account is None:
        return {"status": "error", "detail": "No Instagram accounts are configured for this yet."}

    try:
        cl = _build_client(account)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
    if not cl.user_id:
        return {"status": "error", "detail": f"Account {account['username']} isn't logged in right now."}

    try:
        target_user_id, target_username = _resolve_target(cl, target)
    except Exception as exc:
        return {"status": "error", "detail": f"Could not find that account: {_safe_err(exc, account)}"}

    # Only count against the cooldown once we know the target resolves —
    # a typo shouldn't burn the user's one request for the next 10 minutes.
    _last_request_at[requester_user_id] = time.time()

    try:
        cl.user_follow(target_user_id)
        relationship = cl.user_friendship_v1(target_user_id)
    except Exception as exc:
        detail = f"Could not send the follow request: {_safe_err(exc, account)}"
        stats.insert_follow_request(
            requester_user_id=requester_user_id,
            requester_chat_id=requester_chat_id,
            account_username=account["username"],
            target_input=target,
            target_user_id=target_user_id,
            target_username=target_username,
            status="error",
        )
        return {"status": "error", "detail": detail}

    status = "accepted" if relationship.following else "pending"
    stats.insert_follow_request(
        requester_user_id=requester_user_id,
        requester_chat_id=requester_chat_id,
        account_username=account["username"],
        target_input=target,
        target_user_id=target_user_id,
        target_username=target_username,
        status=status,
    )
    return {"status": status, "account": account["username"], "target_username": target_username}


async def follow_request_poll_loop(application) -> None:
    """Runs for the life of the process — checks pending follow requests and
    notifies requesters once Instagram has actually decided."""
    while True:
        try:
            await _poll_once(application.bot)
        except Exception:
            logger.exception("Follow-request poll iteration failed")
        await asyncio.sleep(_POLL_INTERVAL_SEC)


async def _poll_once(bot) -> None:
    pending = stats.get_pending_follow_requests()
    if not pending:
        return

    by_account: dict[str, list[dict]] = {}
    for row in pending:
        by_account.setdefault(row["account_username"], []).append(row)

    accounts_by_username = {a["username"]: a for a in cfg.get_instagram_accounts()}

    for username, rows in by_account.items():
        account = accounts_by_username.get(username)
        if not account:
            continue
        try:
            cl = await asyncio.to_thread(_build_client, account)
        except Exception as exc:
            logger.warning("Follow-request poll: could not build client for %s: %s", username, exc)
            continue
        if not cl.user_id:
            continue
        for row in rows:
            await _check_one(cl, bot, row)


async def _check_one(cl, bot, row: dict) -> None:
    target_user_id = row["target_user_id"]
    if not target_user_id:
        return
    try:
        relationship = await asyncio.to_thread(cl.user_friendship_v1, target_user_id)
    except Exception as exc:
        logger.debug("Follow-request poll: friendship check failed for request %s: %s", row["id"], exc)
        return

    if relationship.following:
        new_status = "accepted"
    elif not relationship.outgoing_request:
        # Was pending (we only poll rows already in that state) and Instagram
        # no longer shows an outgoing request, but we're still not
        # following — the standard heuristic for "declined/withdrawn"
        # (Instagram exposes no explicit "rejected" flag on this endpoint).
        new_status = "rejected"
    else:
        return  # still genuinely pending, nothing to tell anyone yet

    stats.update_follow_request_status(row["id"], new_status, notified=True)
    chat_id = row["requester_chat_id"]
    target_name = row["target_username"] or row["target_input"]
    if new_status == "accepted":
        text = f"✅ Your follow request to @{target_name} was accepted!"
    else:
        text = f"❌ Your follow request to @{target_name} was declined."
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:
        logger.warning("Could not notify chat %s about follow request %s: %s", chat_id, row["id"], exc)
