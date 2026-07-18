"""Coordinate inline query selections with target-chat messages."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

# result_id -> url (Telegram inline result id max 64 chars)
_URL_BY_RESULT: dict[str, tuple[str, float]] = {}
_PENDING: dict[tuple[int, str], "InlinePending"] = {}

_TTL_SECONDS = 120.0
_MAX_ENTRIES = 500


@dataclass
class InlinePending:
    user_id: int
    url: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: float = field(default_factory=time.monotonic)


def strip_inline_query(query: str) -> str:
    """Remove @bot mentions and extra whitespace from inline query text."""
    text = query.strip()
    text = re.sub(r"@\w+\s*", "", text).strip()
    return text


def store_url(url: str) -> str:
    _purge_expired()
    result_id = uuid4().hex[:16]
    _URL_BY_RESULT[result_id] = (url, time.monotonic())
    return result_id


def pop_url(result_id: str) -> str | None:
    entry = _URL_BY_RESULT.pop(result_id, None)
    if not entry:
        return None
    url, _ = entry
    return url


def register_pending(user_id: int, url: str) -> InlinePending:
    _purge_expired()
    pending = InlinePending(user_id=user_id, url=url)
    _PENDING[(user_id, url)] = pending
    return pending


def mark_message_seen(user_id: int | None, urls: list[str]) -> None:
    if user_id is None:
        return
    _purge_expired()
    for url in urls:
        pending = _PENDING.pop((user_id, url), None)
        if pending:
            pending.event.set()


def _purge_expired() -> None:
    now = time.monotonic()
    for key, (_, ts) in list(_URL_BY_RESULT.items()):
        if now - ts > _TTL_SECONDS:
            _URL_BY_RESULT.pop(key, None)
    if len(_URL_BY_RESULT) > _MAX_ENTRIES:
        oldest = sorted(_URL_BY_RESULT.items(), key=lambda x: x[1][1])[: len(_URL_BY_RESULT) - _MAX_ENTRIES]
        for key, _ in oldest:
            _URL_BY_RESULT.pop(key, None)
    for key, pending in list(_PENDING.items()):
        if now - pending.created_at > _TTL_SECONDS:
            _PENDING.pop(key, None)
