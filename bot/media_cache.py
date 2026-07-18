"""Telegram file_id cache — instant re-sends for previously downloaded links."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

from bot.config import DATA_DIR

logger = logging.getLogger(__name__)

_DB = DATA_DIR / "media_cache.db"
_lock = threading.Lock()
_TTL_SECONDS = 14 * 24 * 3600  # Telegram file_ids usually last ~weeks; refresh within 14d


@dataclass
class CachedMedia:
    file_id: str
    is_audio: bool
    title: str
    file_size: int | None
    is_image: bool = False


def cache_key(url: str) -> str:
    """Stable key: Instagram shortcode / Spotify id / otherwise URL hash."""
    lower = url.lower()
    m = re.search(r"instagram\.com/(?:reel|p|tv)/([^/?#]+)", lower)
    if m:
        return f"ig:{m.group(1)}"
    m = re.search(r"(?:open\.)?spotify\.com/(?:track|episode)/([a-zA-Z0-9]+)", url)
    if m:
        return f"sp:{m.group(1)}"
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{6,})", url)
    if m:
        return f"yt:{m.group(1)}"
    m = re.search(r"tiktok\.com/.*/video/(\d+)", lower)
    if m:
        return f"tt:{m.group(1)}"
    m = re.search(r"(?:twitter\.com|x\.com)/(?:[^/]+/)?status(?:es)?/(\d+)", lower)
    if m:
        return f"x:{m.group(1)}"
    m = re.search(r"pinterest\.[^/]+/pin/(\d+)", lower)
    if m:
        return f"pin:{m.group(1)}"
    m = re.search(r"(?:^|//)(?:www\.)?pin\.it/([A-Za-z0-9]+)", url)
    if m:
        return f"pinit:{m.group(1)}"
    return "u:" + hashlib.sha256(url.strip().encode()).hexdigest()[:24]


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                is_audio INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                file_size INTEGER,
                created_at REAL NOT NULL,
                is_image INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(media_cache)")}
        if "is_image" not in cols:
            conn.execute(
                "ALTER TABLE media_cache ADD COLUMN is_image INTEGER NOT NULL DEFAULT 0"
            )


def get_cached(url: str) -> CachedMedia | None:
    init_db()
    key = cache_key(url)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT file_id, is_audio, title, file_size, created_at, is_image "
            "FROM media_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    if time.time() - row[4] > _TTL_SECONDS:
        delete_cached(url)
        return None
    return CachedMedia(
        file_id=row[0],
        is_audio=bool(row[1]),
        title=row[2] or "media",
        file_size=row[3],
        is_image=bool(row[5]),
    )


def store_cached(
    url: str,
    file_id: str,
    *,
    is_audio: bool,
    title: str,
    file_size: int | None = None,
    is_image: bool = False,
) -> None:
    if not file_id:
        return
    init_db()
    key = cache_key(url)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO media_cache
            (cache_key, file_id, is_audio, title, file_size, created_at, is_image)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (key, file_id, int(is_audio), title[:200], file_size, time.time(), int(is_image)),
        )
    logger.info("Cached file_id for %s (image=%s)", key, is_image)


def delete_cached(url: str) -> None:
    init_db()
    key = cache_key(url)
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM media_cache WHERE cache_key = ?", (key,))


def cache_count() -> int:
    init_db()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM media_cache").fetchone()
    return int(row[0]) if row else 0


def clear_all() -> int:
    """Delete every cached file_id. Returns how many rows were removed."""
    init_db()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM media_cache").fetchone()
        n = int(row[0]) if row else 0
        conn.execute("DELETE FROM media_cache")
    logger.info("Cleared media_cache (%d entries)", n)
    return n


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
