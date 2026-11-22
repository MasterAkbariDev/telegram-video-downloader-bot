"""Offline regressions for Instagram routing, media, and cleanup."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from bot import downloader, instagram, media_cache, urls
from bot.jobs import DownloadCancelledError


URL = "https://www.instagram.com/p/Example/"
PHOTO = "https://scontent.cdninstagram.com/photo.jpg?key=a%2Fb%3D"
VIDEO = "https://scontent.fbcdn.net/video.mp4"


def image_node(url=PHOTO):
    return {"media_type": 1, "image_versions2": {"candidates": [{"url": url, "width": 1000}]}}


class InstagramMediaTest(unittest.TestCase):
    def test_normalizes_supported_host_and_reels_variation(self):
        original = "http://m.instagram.com/example/reels/Some_Ab/?igsh=abc"
        self.assertEqual(urls.extract_urls(original), [original])
        self.assertEqual(
            instagram.normalize_instagram_url(original),
            "https://www.instagram.com/reel/Some_Ab/?igsh=abc",
        )
        self.assertFalse(instagram.is_instagram_url("https://example.com/instagram.com/p/Ab/"))

    def test_cache_key_preserves_shortcode_case(self):
        first = media_cache.cache_key(URL)
        self.assertNotEqual(first, media_cache.cache_key(URL.lower()))
        self.assertEqual(first, media_cache.cache_key("https://instagr.am/reels/Example/"))

    def test_share_redirect_is_canonical_and_cannot_leave_instagram(self):
        share = "https://www.instagram.com/share/reel/Short/"
        alternate = "https://www.instagram.com/share/p/Short/"
        self.assertEqual(instagram.normalize_instagram_url(alternate), alternate)
        for destination, expected in ((URL, URL), ("https://example.com/p/Example/", None)):
            with self.subTest(destination=destination):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(302, headers={"location": destination})
                )
                client = httpx.Client(transport=transport)
                with patch.object(instagram.httpx, "Client", return_value=client):
                    if expected:
                        self.assertEqual(instagram.resolve_instagram_share(share), expected)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "shared link"):
                            instagram.resolve_instagram_share(share)

    def test_mixed_carousel_preserves_order_and_signed_urls(self):
        slides = instagram._slides_from_media_object({
            "media_type": 8,
            "carousel_media": [
                image_node(), {"media_type": 2, "video_versions": [{"url": VIDEO}]},
                image_node(PHOTO + "&second=1"),
            ],
        })
        self.assertEqual([(item.kind, item.url) for item in slides], [
            ("image", PHOTO), ("video", VIDEO), ("image", PHOTO + "&second=1"),
        ])

    def test_incomplete_carousel_does_not_return_its_cover_or_other_items(self):
        slides = instagram._slides_from_media_object({
            **image_node(),
            "media_type": 8,
            "carousel_media": [image_node(), {"media_type": 2, **image_node(), "is_video": True}],
        })
        self.assertEqual(slides, [])

    def test_expired_cdn_url_refreshes_metadata_and_cleans_partial_files(self):
        stale = PHOTO + "&expired=1"
        attempts = []
        jpg = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(jpg, format="JPEG")

        def request_media(request):
            attempts.append(str(request.url))
            if str(request.url) == stale:
                return httpx.Response(403)
            return httpx.Response(200, content=jpg.getvalue())

        payloads = [
            {"slides": [instagram.IgSlide("image", PHOTO), instagram.IgSlide("image", stale)]},
            {"slides": [instagram.IgSlide("image", PHOTO), instagram.IgSlide("image", PHOTO + "&item=2")]},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            real_client = httpx.Client
            with (
                patch.object(instagram, "DOWNLOAD_DIR", path),
                patch.object(downloader, "_instagram_extract", side_effect=payloads) as extract,
                patch.object(instagram.httpx, "Client", side_effect=lambda **_: real_client(
                    transport=httpx.MockTransport(request_media)
                )),
            ):
                result = instagram.resolve_instagram_post(URL)
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(attempts, [PHOTO, stale, PHOTO, PHOTO + "&item=2"])
            self.assertEqual([item.path.name for item in result.album], ["slide_01.jpg", "slide_02.jpg"])
            self.assertEqual(len(list(path.glob("*/*"))), 2)

    def test_cancel_and_timeout_leave_no_temporary_files(self):
        for failure in (DownloadCancelledError(), httpx.ReadTimeout("stream stopped")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)

                def fail(slides, directory, progress, cancel):
                    (directory / "partial.tmp").write_bytes(b"partial")
                    raise failure

                with (
                    patch.object(instagram, "DOWNLOAD_DIR", path),
                    patch.object(downloader, "_instagram_extract", return_value={
                        "slides": [instagram.IgSlide("image", PHOTO)],
                    }),
                    patch.object(instagram, "_download_slides", side_effect=fail) as download,
                ):
                    expected = DownloadCancelledError if isinstance(failure, DownloadCancelledError) else RuntimeError
                    with self.assertRaises(expected):
                        instagram.resolve_instagram_post(URL)
                self.assertEqual(download.call_count, 1 if expected is DownloadCancelledError else 2)
                self.assertEqual(list(path.iterdir()), [])

    def test_invalid_image_and_video_payloads_fail_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "not-media"
            path.write_bytes(b"<html>not media</html>")
            with self.assertRaises(OSError):
                instagram._validate_slide(path, "image")
            with self.assertRaisesRegex(RuntimeError, "invalid video"):
                instagram._validate_slide(path, "video")
