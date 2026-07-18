"""Download statistics and activity logs (SQLite)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bot.config import DOWNLOAD_DIR, STATS_DB

_db_initialized = False


def init_db() -> None:
    """Create tables once at startup — must not call _connect (avoids recursion)."""
    global _db_initialized
    if _db_initialized:
        return

    STATS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATS_DB)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                chat_id INTEGER NOT NULL,
                chat_type TEXT,
                url TEXT NOT NULL,
                platform TEXT,
                file_size INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at DESC)"
        )
        conn.commit()
        _db_initialized = True
    finally:
        conn.close()


@contextmanager
def _connect():
    if not _db_initialized:
        init_db()
    conn = sqlite3.connect(STATS_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def record_download(
    *,
    user_id: int,
    username: str | None,
    chat_id: int,
    chat_type: str | None,
    url: str,
    platform: str,
    file_size: int | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO downloads (user_id, username, chat_id, chat_type, url, platform, file_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, chat_id, chat_type, url, platform, file_size, now),
        )
        conn.commit()


def get_stats_summary() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM downloads").fetchone()[0]
        bytes_total = conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) FROM downloads"
        ).fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE date(created_at) = date('now')"
        ).fetchone()[0]
    return {
        "total_downloads": total,
        "unique_users": users,
        "bytes_total": bytes_total,
        "downloads_today": today,
    }


def get_recent_logs(limit: int = 15) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, url, platform, file_size, created_at, chat_type
            FROM downloads
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def get_disk_info() -> dict:
    import shutil

    usage = shutil.disk_usage(DOWNLOAD_DIR)
    downloads_bytes = dir_size(DOWNLOAD_DIR)
    return {
        "disk_total": usage.total,
        "disk_used": usage.used,
        "disk_free": usage.free,
        "downloads_bytes": downloads_bytes,
    }
