"""Regression tests for Instagram session selection and recovery."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp.utils import DownloadError

from bot import downloader


class InstagramSessionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cookie_files = []
        for name in ("first", "second"):
            path = Path(temp.name) / name
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{name}\n",
                encoding="ascii",
            )
            self.cookie_files.append(str(path))
        self.addCleanup(patch.stopall)
        patch.object(downloader, "_instagram_throttle").start()
        self.saved = patch(
            "bot.instagram_auth.existing_instagram_cookie_files",
            return_value=self.cookie_files,
        ).start()
        self.refresh = patch("bot.instagram_auth.refresh_instagram_cookies", return_value=None).start()
        self.attempts = []

    def run_extraction(self, outcomes):
        def attempt(ydl, url):
            cookies = ydl.params.get("cookiefile")
            session = Path(cookies).read_text().splitlines()[-1].split("\t")[-1] if cookies else None
            self.attempts.append(session)
            if cookies:
                self.assertNotIn(cookies, self.cookie_files)
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

        return downloader._instagram_extract(
            "https://www.instagram.com/p/Example/",
            {"cookiefile": self.cookie_files[0], "auto_init": False},
            extract_attempt=attempt,
        )

    def test_public_success_never_reads_or_refreshes_a_saved_session(self):
        expected = {"id": "Example"}
        self.assertEqual(self.run_extraction(iter([expected])), expected)
        self.assertEqual(self.attempts, [None])
        self.saved.assert_not_called()
        self.refresh.assert_not_called()

    def test_rejected_saved_sessions_rotate_after_login_wall(self):
        expected = {"id": "Example"}
        outcomes = iter([
            DownloadError("[Instagram] Login required"),
            DownloadError("[Instagram] HTTP Error 400: Bad Request"),
            expected,
        ])
        self.assertEqual(self.run_extraction(outcomes), expected)
        self.assertEqual(self.attempts, [None, "first", "second"])
        self.refresh.assert_not_called()
        for name, path in zip(("first", "second"), self.cookie_files):
            self.assertTrue(Path(path).read_text().endswith(f"\tsessionid\t{name}\n"))

    def test_http_rate_limit_does_not_rotate_or_refresh_sessions(self):
        with self.assertRaisesRegex(DownloadError, "429"):
            self.run_extraction(iter([DownloadError("[Instagram] HTTP Error 429")]))
        self.assertEqual(self.attempts, [None])
        self.saved.assert_not_called()
        self.refresh.assert_not_called()

    def test_empty_public_media_does_not_trigger_a_new_login(self):
        with self.assertRaisesRegex(DownloadError, "empty media"):
            self.run_extraction(iter([
                DownloadError("[Instagram] empty media response"),
                DownloadError("[Instagram] HTTP Error 400"),
                DownloadError("[Instagram] HTTP Error 401"),
            ]))
        self.refresh.assert_not_called()

    def test_permanent_not_found_does_not_read_sessions(self):
        with self.assertRaisesRegex(DownloadError, "404"):
            self.run_extraction(iter([DownloadError("[Instagram] HTTP Error 404")]))
        self.saved.assert_not_called()
        self.refresh.assert_not_called()
