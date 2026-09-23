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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                chat_id INTEGER,
                chat_type TEXT,
                url TEXT NOT NULL,
                host TEXT,
                platform TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failures_created ON download_failures(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failures_kind ON download_failures(kind)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS follow_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_user_id INTEGER,
                requester_chat_id INTEGER,
                account_username TEXT NOT NULL,
                target_input TEXT,
                target_user_id TEXT,
                target_username TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_follow_requests_status ON follow_requests(status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
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


def record_failure(
    *,
    kind: str,
    url: str,
    error: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
    chat_id: int | None = None,
    chat_type: str | None = None,
    platform: str | None = None,
) -> None:
    """Persist unsupported-link or failed-download events for the admin panel."""
    from urllib.parse import urlparse

    now = datetime.now(timezone.utc).isoformat()
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        host = ""
    err = (error or "")[:500]
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO download_failures
                (kind, user_id, username, chat_id, chat_type, url, host, platform, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                user_id,
                username,
                chat_id,
                chat_type,
                url[:2000],
                host[:200],
                platform,
                err,
                now,
            ),
        )
        conn.commit()


def get_recent_failures(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT kind, user_id, username, url, host, platform, error, created_at, chat_type
            FROM download_failures
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_failure_host_counts(limit: int = 12) -> list[dict]:
    """Top hosts among unsupported / failed downloads (what to consider adding or fixing)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT host, kind, COUNT(*) AS n
            FROM download_failures
            WHERE host IS NOT NULL AND host != ''
            GROUP BY host, kind
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_failures() -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM download_failures")
        conn.commit()
        return cur.rowcount


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


# ---- Follow requests (private-account requests, any bot user) ----


def insert_follow_request(
    *,
    requester_user_id: int,
    requester_chat_id: int,
    account_username: str,
    target_input: str,
    target_user_id: str | None,
    target_username: str | None,
    status: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO follow_requests
                (requester_user_id, requester_chat_id, account_username, target_input,
                 target_user_id, target_username, status, created_at, updated_at, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                requester_user_id, requester_chat_id, account_username, target_input,
                target_user_id, target_username, status, now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_follow_requests() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM follow_requests WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_follow_requests(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM follow_requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_follow_request_counts(account_username: str | None = None) -> dict:
    """{'pending': n, 'accepted': n, 'rejected': n, 'error': n}."""
    with _connect() as conn:
        if account_username:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM follow_requests WHERE account_username = ? GROUP BY status",
                (account_username,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM follow_requests GROUP BY status"
            ).fetchall()
    counts = {"pending": 0, "accepted": 0, "rejected": 0, "error": 0}
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


def update_follow_request_status(request_id: int, status: str, *, notified: bool | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if notified is None:
            conn.execute(
                "UPDATE follow_requests SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, request_id),
            )
        else:
            conn.execute(
                "UPDATE follow_requests SET status = ?, updated_at = ?, notified = ? WHERE id = ?",
                (status, now, int(notified), request_id),
            )
        conn.commit()


# ---- Per-group enable/disable (group admins can turn link-downloading off) ----


def is_group_enabled(chat_id: int) -> bool:
    """Absence of a row means enabled — only disabled groups need one."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM group_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return True if row is None else bool(row["enabled"])


def set_group_enabled(chat_id: int, enabled: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO group_settings (chat_id, enabled, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (chat_id, int(enabled), now),
        )
        conn.commit()
