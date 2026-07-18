"""Track in-progress downloads/uploads per user for /cancel."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Callable

CancelCheck = Callable[[], None]


class DownloadCancelledError(Exception):
    """Raised when the user cancels an active download or upload."""


@dataclass
class UserJob:
    user_id: int
    url: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise DownloadCancelledError()

    def cancel_check(self) -> CancelCheck:
        return self.check


_lock = threading.Lock()
_jobs: dict[int, UserJob] = {}


def register_job(user_id: int, url: str, *, task: asyncio.Task | None = None) -> UserJob:
    job = UserJob(user_id=user_id, url=url, task=task)
    with _lock:
        _jobs[user_id] = job
    return job


def unregister_job(user_id: int) -> None:
    with _lock:
        _jobs.pop(user_id, None)


def get_job(user_id: int) -> UserJob | None:
    with _lock:
        return _jobs.get(user_id)


def is_cancelled(user_id: int) -> bool:
    job = get_job(user_id)
    return bool(job and job.cancel_event.is_set())


def request_cancel(user_id: int) -> bool:
    with _lock:
        job = _jobs.get(user_id)
        if not job:
            return False
        job.cancel_event.set()
        task = job.task
    if task and not task.done():
        task.cancel()
    return True
