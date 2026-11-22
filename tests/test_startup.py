"""Ensure network maintenance cannot block Telegram startup."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import __main__ as app


class StartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_is_started_in_background_and_cancelled_on_stop(self):
        application = SimpleNamespace(bot_data={})
        pending = asyncio.Event()

        async def wait_for_network(_application):
            await pending.wait()

        with (
            patch.object(app.media_cache, "init_db"),
            patch.object(app, "notify_pending_update", side_effect=wait_for_network),
            patch.object(app, "update_check_loop", side_effect=wait_for_network),
            patch.object(app, "follow_request_poll_loop", new_callable=AsyncMock),
            patch.object(app, "ytdlp_update_loop", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(app._post_init(application), timeout=1)
            tasks = application.bot_data["maintenance_tasks"]
            self.assertFalse(tasks[0].done())
            self.assertFalse(tasks[1].done())
            await app._post_stop(application)

        self.assertTrue(all(task.done() for task in tasks))
        self.assertNotIn("maintenance_tasks", application.bot_data)
