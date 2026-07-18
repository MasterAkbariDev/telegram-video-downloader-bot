"""In-memory pending quality-picker state (TTL)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

_TTL_SECONDS = 600.0
_MAX_ENTRIES = 200

_PENDING: dict[str, "PendingQuality"] = {}


@dataclass
class PendingQuality:
    token: str
    user_id: int
    url: str
    chat_id: int
    reply_to_message_id: int
    index: int
    total: int
    heights: list[int]
    title: str | None
    uploader: str | None
    duration: int | None
    thumbnail: str | None
    info: dict[str, Any] | None = None
    picker_message_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > _TTL_SECONDS


def create(
    *,
    user_id: int,
    url: str,
    chat_id: int,
    reply_to_message_id: int,
    index: int,
    total: int,
    heights: list[int],
    title: str | None,
    uploader: str | None,
    duration: int | None,
    thumbnail: str | None,
    info: dict[str, Any] | None,
) -> PendingQuality:
    _purge()
    token = uuid4().hex[:10]
    pending = PendingQuality(
        token=token,
        user_id=user_id,
        url=url,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        index=index,
        total=total,
        heights=heights,
        title=title,
        uploader=uploader,
        duration=duration,
        thumbnail=thumbnail,
        info=info,
    )
    _PENDING[token] = pending
    return pending


def get(token: str) -> PendingQuality | None:
    _purge()
    pending = _PENDING.get(token)
    if not pending or pending.expired:
        _PENDING.pop(token, None)
        return None
    return pending


def pop(token: str) -> PendingQuality | None:
    _purge()
    return _PENDING.pop(token, None)


def clear_user(user_id: int) -> int:
    """Remove all pending pickers for a user. Returns how many were cleared."""
    _purge()
    tokens = [t for t, p in _PENDING.items() if p.user_id == user_id]
    for t in tokens:
        _PENDING.pop(t, None)
    return len(tokens)


def set_picker_message_id(token: str, message_id: int) -> None:
    pending = _PENDING.get(token)
    if pending:
        pending.picker_message_id = message_id


def _purge() -> None:
    now = time.monotonic()
    for token, pending in list(_PENDING.items()):
        if now - pending.created_at > _TTL_SECONDS:
            _PENDING.pop(token, None)
    if len(_PENDING) > _MAX_ENTRIES:
        oldest = sorted(_PENDING.items(), key=lambda x: x[1].created_at)[
            : len(_PENDING) - _MAX_ENTRIES
        ]
        for token, _ in oldest:
            _PENDING.pop(token, None)
