"""Unit tests for access-token caching.

Runs offline: ApiClient is mocked, no network and no real credentials.

    python -m unittest discover tests
"""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click

from famly_fetch.downloader import (
    build_api_client,
    read_token_cache,
    write_token_cache,
)


def _client(*, valid=True, login_token="fresh-token"):
    """A fake ApiClient whose me_me_me() reports the token as valid or not."""
    client = mock.Mock()
    client.me_me_me.return_value = {"loginId": "x"} if valid else None
    client.access_token = login_token
    return client


class TestTokenCacheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "token"

    def test_missing_file_reads_as_none(self):
        self.assertIsNone(read_token_cache(self.path))

    def test_directory_reads_as_none_rather_than_raising(self):
        self.assertIsNone(read_token_cache(Path(self.tmp.name)))

    def test_empty_file_reads_as_none(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("   \n")
        self.assertIsNone(read_token_cache(self.path))

    def test_roundtrip_strips_whitespace(self):
        write_token_cache(self.path, "abc123")
        self.assertEqual(self.path.read_text(), "abc123")
        self.assertEqual(read_token_cache(self.path), "abc123")

    def test_written_file_is_owner_only(self):
        write_token_cache(self.path, "abc123")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"token cache is {oct(mode)}, expected 0o600")

    def test_overwrite_truncates(self):
        write_token_cache(self.path, "a-long-stale-token")
        write_token_cache(self.path, "short")
        self.assertEqual(read_token_cache(self.path), "short")


class TestBuildApiClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "token"

    def _build(self, fake_client, **kwargs):
        kwargs.setdefault("base_url", "https://app.famly.co")
        kwargs.setdefault("user_agent", None)
        kwargs.setdefault("email", "parent@example.com")
        kwargs.setdefault("password", "hunter2")
        kwargs.setdefault("access_token", None)
        kwargs.setdefault("token_cache", self.path)
        with mock.patch(
            "famly_fetch.downloader.ApiClient", return_value=fake_client
        ) as factory:
            self.factory = factory
            return build_api_client(**kwargs)

    def test_valid_cached_token_skips_login(self):
        write_token_cache(self.path, "cached-token")
        client = _client(valid=True)
        self._build(client)
        client.login.assert_not_called()
        self.assertEqual(self.factory.call_args.kwargs["access_token"], "cached-token")

    def test_valid_cached_token_leaves_cache_alone(self):
        write_token_cache(self.path, "cached-token")
        self._build(_client(valid=True))
        self.assertEqual(read_token_cache(self.path), "cached-token")

    def test_rejected_cached_token_triggers_login_and_rewrite(self):
        write_token_cache(self.path, "stale-token")
        client = _client(valid=False, login_token="fresh-token")
        self._build(client)
        client.login.assert_called_once_with("parent@example.com", "hunter2")
        self.assertEqual(read_token_cache(self.path), "fresh-token")

    def test_no_cache_file_logs_in_and_writes_one(self):
        client = _client(login_token="fresh-token")
        self._build(client)
        client.login.assert_called_once()
        self.assertEqual(read_token_cache(self.path), "fresh-token")

    def test_explicit_access_token_wins_and_is_not_cached(self):
        client = _client()
        self._build(client, access_token="supplied-token")
        client.login.assert_not_called()
        client.me_me_me.assert_not_called()
        self.assertEqual(
            self.factory.call_args.kwargs["access_token"], "supplied-token"
        )
        self.assertFalse(self.path.exists())

    def test_without_token_cache_behaviour_is_unchanged(self):
        client = _client()
        self._build(client, token_cache=None)
        client.login.assert_called_once()
        client.me_me_me.assert_not_called()

    def test_stale_cache_without_credentials_is_a_clear_error(self):
        write_token_cache(self.path, "stale-token")
        with self.assertRaises(click.ClickException) as caught:
            self._build(_client(valid=False), email=None, password=None)
        self.assertIn("no email/password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
