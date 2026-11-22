"""Offline regression coverage for X media order and Telegram upload retry."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, RetryAfter

from bot import pinterest, twitter, uploader


class ProviderTest(unittest.TestCase):
    def test_ordered_x_media_keeps_images_and_distinct_videos(self):
        media = [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/first.jpg"},
            {"type": "video", "video_info": {"variants": [
                {"content_type": "video/mp4", "bitrate": 128, "url": "https://video.twimg.com/low.mp4"},
                {"content_type": "video/mp4", "bitrate": 256, "url": "https://video.twimg.com/best.mp4"},
            ]}},
            {"type": "video", "video_info": {"variants": [
                {"content_type": "video/mp4", "bitrate": 128, "url": "https://video.twimg.com/second.mp4"},
            ]}},
        ]
        with patch.object(twitter, "_fetch_tweet", return_value={"mediaDetails": media}):
            result = twitter.resolve_x_post("https://twitter.com/user/status/1234")
        self.assertEqual([item.kind for item in result.album], ["image", "video", "video"])
        self.assertEqual([item.url for item in result.album][1:], [
            "https://video.twimg.com/best.mp4", "https://video.twimg.com/second.mp4",
        ])

    def test_pinterest_network_failure_can_reach_fallback(self):
        with patch.object(pinterest, "scrape_pinterest_pin", side_effect=httpx.ReadTimeout("slow")):
            self.assertIsNone(pinterest.resolve_pinterest("https://www.pinterest.com/pin/1234/"))


class TelegramDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_rate_limit_retries_once(self):
        send = AsyncMock(side_effect=[RetryAfter(1), "receipt"])
        with patch.object(uploader.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await uploader._send_after_rate_limit(send), "receipt")
        sleep.assert_awaited_once_with(1)
        self.assertEqual(send.await_count, 2)

    async def test_ambiguous_network_error_is_not_retried(self):
        send = AsyncMock(side_effect=NetworkError("connection closed"))
        with self.assertRaises(NetworkError):
            await uploader._send_after_rate_limit(send)
        send.assert_awaited_once()
