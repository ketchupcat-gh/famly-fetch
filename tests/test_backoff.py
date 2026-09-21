"""Unit tests for the backoff retry helpers.

Runs offline: urlopen and time.sleep are mocked.

    python -m unittest discover tests
"""

import http.client
import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from famly_fetch.api_client import (
    download_to_file_with_backoff,
    read_body_with_backoff,
    urlopen_with_backoff,
)


class _FakeResponse(io.BytesIO):
    """A urlopen response that delivers its whole body."""

    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _TruncatedResponse:
    """A response that delivers `prefix`, then dies mid-transfer.

    This is the shape of the real failure: urlopen() succeeds, the status is
    200, and the connection drops while the body is being read.
    """

    def __init__(self, prefix, error, status=200):
        self._chunks = [prefix] if prefix else []
        self._error = error
        self.status = status

    def read(self, *args):
        if self._chunks:
            return self._chunks.pop(0)
        raise self._error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        url="https://app.famly.co/api",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(b""),
    )


def _fail_then(errors, result):
    """fake urlopen raising each error once, then returning result."""
    remaining = list(errors)

    def fake_urlopen(req):
        if remaining:
            raise remaining.pop(0)
        return result

    return fake_urlopen


class TestUrlopenWithBackoff(unittest.TestCase):
    def _run(self, fake_urlopen, **kwargs):
        """Run with urlopen/sleep patched; sleeps recorded on self.sleeps."""
        self.sleeps = []
        with (
            mock.patch("famly_fetch.api_client.urllib.request.urlopen", fake_urlopen),
            mock.patch("famly_fetch.api_client.time.sleep", self.sleeps.append),
        ):
            return urlopen_with_backoff("req", **kwargs)

    def test_retries_on_429_then_succeeds(self):
        sentinel = object()
        result = self._run(_fail_then([_http_error(429), _http_error(429)], sentinel))
        self.assertIs(result, sentinel)
        # exponential: base delay 2s doubling per attempt
        self.assertEqual(self.sleeps, [2.0, 4.0])

    def test_honors_retry_after_header(self):
        result = self._run(_fail_then([_http_error(429, retry_after=7)], "ok"))
        self.assertEqual(result, "ok")
        self.assertEqual(self.sleeps, [7.0])

    def test_negative_retry_after_clamped_to_zero(self):
        result = self._run(_fail_then([_http_error(429, retry_after=-5)], "ok"))
        self.assertEqual(result, "ok")
        self.assertEqual(self.sleeps, [0.0])

    def test_does_not_retry_client_errors(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._run(_fail_then([_http_error(400)] * 5, "unreached"))
        self.assertEqual(self.sleeps, [])

    def test_raises_after_exhausting_attempts(self):
        calls = {"n": 0}

        def fake_urlopen(req):
            calls["n"] += 1
            raise urllib.error.URLError("connection reset")

        with self.assertRaises(urllib.error.URLError):
            self._run(fake_urlopen, attempts=3)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_retries_on_incomplete_read(self):
        err = http.client.IncompleteRead(partial=b"")
        result = self._run(_fail_then([err], "ok"))
        self.assertEqual(result, "ok")
        self.assertEqual(self.sleeps, [2.0])


class TestDownloadToFileWithBackoff(unittest.TestCase):
    """The transfer-level retry: covers the body read, not just the connect."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "img.jpg")
        self.sleeps = []

    def _run(self, fake_urlopen, **kwargs):
        with (
            mock.patch("famly_fetch.api_client.urllib.request.urlopen", fake_urlopen),
            mock.patch("famly_fetch.api_client.time.sleep", self.sleeps.append),
        ):
            return download_to_file_with_backoff("req", self.path, **kwargs)

    def test_writes_body_to_file(self):
        self._run(_fail_then([], _FakeResponse(b"jpegbytes")))
        self.assertEqual(self.path.read_bytes(), b"jpegbytes")
        self.assertEqual(self.sleeps, [])

    def test_retries_truncated_transfer_and_writes_whole_file(self):
        """The Sept 21 failure: IncompleteRead partway through an image.

        The old code retried only urlopen(), so this escaped entirely and
        aborted the run. The retry must now cover it, and the bytes from the
        failed attempt must not survive into the finished file.
        """
        truncated = _TruncatedResponse(
            b"half-", http.client.IncompleteRead(partial=b"half-")
        )
        responses = [truncated, _FakeResponse(b"whole-image")]

        def fake_urlopen(req):
            return responses.pop(0)

        with (
            mock.patch("famly_fetch.api_client.urllib.request.urlopen", fake_urlopen),
            mock.patch("famly_fetch.api_client.time.sleep", self.sleeps.append),
        ):
            download_to_file_with_backoff("req", self.path, attempts=3)

        self.assertEqual(self.path.read_bytes(), b"whole-image")
        self.assertEqual(self.sleeps, [2.0])

    def test_leaves_no_partial_file_after_final_failure(self):
        def fake_urlopen(req):
            return _TruncatedResponse(
                b"stub", http.client.IncompleteRead(partial=b"stub")
            )

        with self.assertRaises(http.client.IncompleteRead):
            self._run(fake_urlopen, attempts=2)
        self.assertFalse(
            self.path.exists(),
            "a failed download must not leave bytes that look like a real one",
        )

    def test_does_not_retry_client_errors(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._run(_fail_then([_http_error(404)] * 3, "unreached"))
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.path.exists())

    def test_retries_on_429(self):
        self._run(_fail_then([_http_error(429)], _FakeResponse(b"ok")))
        self.assertEqual(self.path.read_bytes(), b"ok")
        self.assertEqual(self.sleeps, [2.0])

    def test_non_200_raises_and_writes_nothing(self):
        with self.assertRaisesRegex(Exception, "Broken!"):
            self._run(_fail_then([], _FakeResponse(b"nope", status=500)))
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.path.exists())


class TestReadBodyWithBackoff(unittest.TestCase):
    """The JSON API path, which had the same body-read gap as the downloads."""

    def setUp(self):
        self.sleeps = []

    def _run(self, fake_urlopen, **kwargs):
        with (
            mock.patch("famly_fetch.api_client.urllib.request.urlopen", fake_urlopen),
            mock.patch("famly_fetch.api_client.time.sleep", self.sleeps.append),
        ):
            return read_body_with_backoff("req", **kwargs)

    def test_returns_status_and_decoded_body(self):
        status, body = self._run(_fail_then([], _FakeResponse(b'{"ok":true}')))
        self.assertEqual((status, body), (200, '{"ok":true}'))
        self.assertEqual(self.sleeps, [])

    def test_retries_truncated_body(self):
        responses = [
            _TruncatedResponse(b"", http.client.IncompleteRead(partial=b"")),
            _FakeResponse(b'{"ok":true}'),
        ]
        status, body = self._run(lambda req: responses.pop(0), attempts=3)
        self.assertEqual(body, '{"ok":true}')
        self.assertEqual(self.sleeps, [2.0])


if __name__ == "__main__":
    unittest.main()
