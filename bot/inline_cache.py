"""Inline mode: in-flight prepare registry → cached file_id → tap to send."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from bot import media_cache

_TTL_SECONDS = 600.0
_MAX_ENTRIES = 500
# How long to keep an error so we don't thrash the same broken link
_ERROR_COOLDOWN_SECONDS = 120.0

# Background prepare status keyed by media_cache.cache_key(url)
_PREPARE: dict[str, "PrepareState"] = {}
# In-flight asyncio tasks — one prepare per URL key
_TASKS: dict[str, asyncio.Task] = {}


@dataclass
class PrepareState:
    url: str
    user_id: int
    status: str = "preparing"  # preparing | ready | error
    error: str | None = None
    title: str | None = None
    created_at: float = field(default_factory=time.monotonic)


def strip_inline_query(query: str) -> str:
    """Remove @bot mentions and extra whitespace from inline query text."""
    text = query.strip()
    text = re.sub(r"@\w+\s*", "", text).strip()
    return text


def prepare_key(url: str) -> str:
    return media_cache.cache_key(url)


def get_prepare(url: str) -> PrepareState | None:
    _purge_expired()
    return _PREPARE.get(prepare_key(url))


def get_task(url: str) -> asyncio.Task | None:
    key = prepare_key(url)
    task = _TASKS.get(key)
    if task is None:
        return None
    if task.done():
        _TASKS.pop(key, None)
        return None
    return task


def is_preparing(url: str) -> bool:
    task = get_task(url)
    if task is not None:
        return True
    state = get_prepare(url)
    return bool(state and state.status == "preparing")


def begin_prepare(url: str, user_id: int) -> PrepareState | None:
    """Mark URL as preparing. Returns None if already in-flight, cached, or cooling down."""
    _purge_expired()
    if media_cache.get_cached(url):
        return None
    key = prepare_key(url)
    task = _TASKS.get(key)
    if task is not None and not task.done():
        state = _PREPARE.get(key)
        if state:
            state.user_id = user_id
        return None
    existing = _PREPARE.get(key)
    if existing and existing.status == "preparing":
        existing.user_id = user_id
        return None
    # Already prepared this session — do not re-download
    if existing and existing.status == "ready":
        return None
    # Don't restart failed links immediately (stops album/error spam)
    if existing and existing.status == "error":
        if time.monotonic() - existing.created_at < _ERROR_COOLDOWN_SECONDS:
            return None
    state = PrepareState(url=url, user_id=user_id)
    _PREPARE[key] = state
    return state


def register_task(url: str, task: asyncio.Task) -> None:
    key = prepare_key(url)
    _TASKS[key] = task

    def _cleanup(t: asyncio.Task) -> None:
        cur = _TASKS.get(key)
        if cur is t:
            _TASKS.pop(key, None)

    task.add_done_callback(_cleanup)


def mark_prepare_ready(url: str, *, title: str | None = None) -> PrepareState | None:
    state = _PREPARE.get(prepare_key(url))
    if not state:
        return None
    state.status = "ready"
    state.title = title
    state.error = None
    return state


def mark_prepare_error(url: str, error: str) -> PrepareState | None:
    key = prepare_key(url)
    state = _PREPARE.get(key)
    if not state:
        state = PrepareState(url=url, user_id=0, status="error", error=error)
        _PREPARE[key] = state
        return state
    state.status = "error"
    state.error = error
    state.created_at = time.monotonic()  # start cooldown from failure time
    return state


def clear_prepare(url: str) -> None:
    """Drop prepare bookkeeping only — never cancel an in-flight download."""
    key = prepare_key(url)
    state = _PREPARE.get(key)
    # Keep error/ready markers; only clear preparing leftovers when cached
    if state and state.status == "preparing" and media_cache.get_cached(url):
        _PREPARE.pop(key, None)
    elif state and state.status == "ready":
        _PREPARE.pop(key, None)
    # Do not cancel _TASKS here — a concurrent waiter may still need the result


async def wait_until_ready(url: str, *, timeout: float = 9.0) -> bool:
    """Wait until media_cache has the URL or prepare finishes/fails. Returns True if cached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if media_cache.get_cached(url):
            return True
        state = get_prepare(url)
        if state and state.status in {"ready", "error"}:
            return bool(media_cache.get_cached(url))
        task = get_task(url)
        if task is None and (not state or state.status != "preparing"):
            # Nothing running and not preparing — stop early
            return bool(media_cache.get_cached(url))
        await asyncio.sleep(0.2)
    return bool(media_cache.get_cached(url))


def _purge_expired() -> None:
    now = time.monotonic()
    for key, state in list(_PREPARE.items()):
        if now - state.created_at > _TTL_SECONDS:
            _PREPARE.pop(key, None)
    for key, task in list(_TASKS.items()):
        if task.done():
            _TASKS.pop(key, None)
    if len(_PREPARE) > _MAX_ENTRIES:
        oldest = sorted(_PREPARE.items(), key=lambda x: x[1].created_at)[
            : len(_PREPARE) - _MAX_ENTRIES
        ]
        for key, _ in oldest:
            _PREPARE.pop(key, None)
