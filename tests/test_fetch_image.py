"""Unit test for FamlyDownloader.fetch_image EXIF writing.

Runs offline: the image download is mocked with an in-memory 1x1 JPEG.

    python -m unittest discover tests
"""

import base64
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import piexif
import piexif.helper

from famly_fetch.downloader import FamlyDownloader
from famly_fetch.image import Image

# 1x1 white baseline JPEG (no EXIF), generated with Pillow.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
    "CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
    "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q=="
)


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FetchImageExifTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)
        self.downloader = FamlyDownloader(
            email="",
            password="",
            famly_base_url="https://app.famly.co",
            pictures_folder=tmp_path / "pictures",
            stop_on_existing=False,
            text_comments=True,
            state_file=tmp_path / "state.json",
            access_token="dummy-token",  # skips login()
        )
        self.file_path = tmp_path / "photo.jpg"

    def fetch(self, text):
        img = Image(
            img_id="00000000-0000-0000-0000-000000000000",
            prefix="https://example.invalid",
            width=1,
            height=1,
            key="photo.jpg",
            date=datetime(2026, 8, 25, 17, 0, 0, tzinfo=timezone.utc),
            text=text,
        )
        with mock.patch(
            # fetch_image downloads via api_client.download_to_file_with_backoff,
            # so that is where urlopen is now reached from.
            "famly_fetch.api_client.urllib.request.urlopen",
            return_value=FakeResponse(TINY_JPEG),
        ):
            self.downloader.fetch_image(img, self.file_path)
        return piexif.load(str(self.file_path))

    def test_text_lands_in_user_comment_and_image_description(self):
        text = "Today we painted with autumn leaves éà"
        exif = self.fetch(text)
        self.assertEqual(
            piexif.helper.UserComment.load(exif["Exif"][piexif.ExifIFD.UserComment]),
            text,
        )
        self.assertEqual(
            exif["0th"][piexif.ImageIFD.ImageDescription].decode("utf-8"),
            text,
        )

    def test_no_text_writes_neither_tag(self):
        exif = self.fetch(None)
        self.assertNotIn(piexif.ExifIFD.UserComment, exif["Exif"])
        self.assertNotIn(piexif.ImageIFD.ImageDescription, exif["0th"])


if __name__ == "__main__":
    unittest.main()
