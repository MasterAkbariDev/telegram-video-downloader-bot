"""Regression tests for the direct Instagram delivery path."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, NetworkError

from bot import downloader, instagram, uploader


URL = "https://www.instagram.com/p/Example/"
CDN_URL = "https://scontent.cdninstagram.com/image.jpg"


class InstagramFastResolutionTest(unittest.TestCase):
    def test_public_resolution_defers_local_download_and_reuses_metadata(self):
        slides = [instagram.IgSlide("image", CDN_URL)]

        def download(items, directory, *args, **kwargs):
            self.assertEqual(items, slides)
            path = directory / "photo.jpg"
            path.write_bytes(b"verified")
            return [downloader.AlbumItem("image", path=path, file_size=8)]

        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            with (
                patch.object(instagram, "DOWNLOAD_DIR", downloads),
                patch.object(downloader, "_instagram_extract", return_value={
                    "slides": slides, "anonymous": True,
                }) as extract,
                patch.object(instagram, "_download_slides", side_effect=download) as download_slides,
            ):
                fast = instagram.resolve_instagram_post(URL, prefer_telegram_url=True)
                self.assertEqual(fast.direct_url, CDN_URL)
                self.assertEqual(list(downloads.iterdir()), [])
                download_slides.assert_not_called()
                materialized = fast.telegram_url_fallback(None, None)
                self.assertTrue(materialized.file_path.is_file())
                self.assertIsNone(materialized.direct_url)
                extract.assert_called_once()

    def test_saved_sessions_and_oversized_album_groups_use_local_delivery(self):
        for anonymous, count in ((False, 1), (True, 11)):
            with self.subTest(anonymous=anonymous, count=count), tempfile.TemporaryDirectory() as directory:
                slides = [instagram.IgSlide("image", CDN_URL)] * count
                files = [downloader.AlbumItem("image", path=Path(directory) / "photo.jpg", file_size=8)] * count
                with (
                    patch.object(instagram, "DOWNLOAD_DIR", Path(directory)),
                    patch.object(downloader, "_instagram_extract", return_value={
                        "slides": slides, "anonymous": anonymous,
                    }),
                    patch.object(instagram, "_download_slides", return_value=files) as download,
                ):
                    result = instagram.resolve_instagram_post(URL, prefer_telegram_url=True)
                self.assertIsNone(result.telegram_url_fallback)
                download.assert_called_once()


class InstagramFastUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_url_receipt_skips_the_local_download(self):
        fallback = Mock()
        photo = SimpleNamespace(photo=[SimpleNamespace(file_id="uploaded-photo")])
        message = SimpleNamespace(reply_photo=AsyncMock(return_value=photo))
        result = downloader.MediaResult(
            "Instagram post", False, None, direct_url=CDN_URL, is_image=True,
            telegram_url_fallback=fallback,
        )

        self.assertEqual(await uploader.send_media(message, result, ""), "uploaded-photo")
        message.reply_photo.assert_awaited_once_with(photo=CDN_URL, read_timeout=25)
        fallback.assert_not_called()

    async def test_explicit_rejection_uploads_validated_file_and_cleans_it(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            job.mkdir()
            path = job / "photo.jpg"
            path.write_bytes(b"verified")
            local = downloader.MediaResult("Instagram post", False, 8, file_path=path, is_image=True)
            fallback = Mock(return_value=local)
            photo = SimpleNamespace(photo=[SimpleNamespace(file_id="uploaded-photo")])
            message = SimpleNamespace(reply_photo=AsyncMock(side_effect=[BadRequest("URL rejected"), photo]))
            result = downloader.MediaResult(
                "Instagram post", False, None, direct_url=CDN_URL, is_image=True,
                telegram_url_fallback=fallback,
            )

            self.assertEqual(await uploader.send_media(message, result, ""), "uploaded-photo")
            self.assertEqual(message.reply_photo.await_count, 2)
            fallback.assert_called_once()
            self.assertFalse(job.exists())

    async def test_ambiguous_network_failure_does_not_send_twice(self):
        fallback = Mock()
        message = SimpleNamespace(reply_photo=AsyncMock(side_effect=NetworkError("response lost")))
        result = downloader.MediaResult(
            "Instagram post", False, None, direct_url=CDN_URL, is_image=True,
            telegram_url_fallback=fallback,
        )
        with self.assertRaises(NetworkError):
            await uploader.send_media(message, result, "")
        message.reply_photo.assert_awaited_once()
        fallback.assert_not_called()
