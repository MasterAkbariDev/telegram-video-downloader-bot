"""Offline regression coverage for X media order and Telegram upload retry."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, RetryAfter

from bot import pinterest, twitter, uploader, urls


class ProviderTest(unittest.TestCase):
    def test_ordered_x_media_keeps_images_and_distinct_videos(self):
        media = [
            {"type": "photo", "url": "https://t.co/permalink",
             "media_url_https": "https://pbs.twimg.com/media/first.jpg"},
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
        self.assertEqual(result.album[0].url, "https://pbs.twimg.com/media/first.jpg?name=orig")
        self.assertEqual([item.url for item in result.album][1:], [
            "https://video.twimg.com/best.mp4", "https://video.twimg.com/second.mp4",
        ])

    def test_pinterest_regional_links_keep_their_complete_hostname(self):
        for url in (
            "https://co.pinterest.com/pin/824721750502199491/",
            "https://www.pinterest.co.uk/pin/824721750502199491/",
            "https://pin.it/Example",
        ):
            with self.subTest(url=url):
                self.assertEqual(urls.extract_urls(url), [url])
                self.assertTrue(pinterest.is_pinterest_url(url))
        self.assertEqual(urls.extract_urls("https://pinterest.unrelated.com/pin/1234/"), [])

    def test_pinterest_image_pin_ignores_related_video(self):
        video = "https://v1.pinimg.com/videos/720p/related.mp4"
        image = '<meta property="og:image" content="https://i.pinimg.com/736x/example.jpg">'
        body = f'<html>{image}<script type="application/json">{{"related":"{video}"}}</script></html>'
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        with patch.object(pinterest.httpx, "Client", return_value=httpx.Client(transport=transport)):
            result = pinterest.resolve_pinterest("https://www.pinterest.com/pin/1234/")
        self.assertTrue(result.is_image)
        self.assertEqual(result.direct_url, "https://i.pinimg.com/originals/example.jpg")

    def test_pinterest_video_pin_uses_its_own_progressive_file(self):
        own = "https://v1.pinimg.com/videos/480p/current.mp4"
        related = "https://v1.pinimg.com/videos/720p/related.mp4"
        body = (
            f'<script type="application/json">{{"related":"{related}"}}</script>'
            f'<script type="application/ld+json" data-test-id="video-snippet">'
            f'{{"@type":"VideoObject","contentUrl":"{own}"}}</script>'
        )
        self.assertEqual(pinterest._pin_video(body), (True, own))

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
