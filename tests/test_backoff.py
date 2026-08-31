"""Unit tests for urlopen_with_backoff retry behavior.

Runs offline: urlopen and time.sleep are mocked.

    python -m unittest discover tests
"""

import io
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from famly_fetch.api_client import urlopen_with_backoff


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


if __name__ == "__main__":
    unittest.main()
