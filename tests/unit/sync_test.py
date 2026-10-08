# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from termius.runtime import Runtime
from termius.sync import (
    DEFAULT_SYNC_TTL, ensure_fresh, is_stale, parse_last_synced, sync_ttl,
)
from termius.vault import VaultPasswordRequired, remember
from termius.core.exceptions import NotSignedIn


class ParseLastSyncedTest(unittest.TestCase):
    def test_iso(self):
        parsed = parse_last_synced('2024-09-19T01:00:00+00:00')
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_zulu(self):
        parsed = parse_last_synced('2024-09-19T01:00:00Z')
        self.assertIsNotNone(parsed)

    def test_unix(self):
        parsed = parse_last_synced('1726700000')
        self.assertIsNotNone(parsed)

    def test_empty(self):
        self.assertIsNone(parse_last_synced(''))
        self.assertIsNone(parse_last_synced(None))


class EnsureFreshTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runtime = Runtime(directory_path=self.tmpdir.name)
        self._ttl = os.environ.pop('TERMIUS_SYNC_TTL', None)
        self._vault = os.environ.pop('TERMIUS_VAULT_PASSWORD', None)

    def tearDown(self):
        self._restore('TERMIUS_SYNC_TTL', self._ttl)
        self._restore('TERMIUS_VAULT_PASSWORD', self._vault)
        self.tmpdir.cleanup()

    def _restore(self, key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _sign_in(self):
        self.runtime.config.set('User', 'username', 'you@example.com')
        self.runtime.config.set('User', 'apikey', 'token')
        self.runtime.config.set('User', 'salt', 'c2FsdA==')
        self.runtime.config.set('User', 'hmac_salt', 'aG1hYw==')
        self.runtime.config.write()

    def test_not_signed_in(self):
        with self.assertRaises(NotSignedIn):
            ensure_fresh(self.runtime)

    def test_missing_password(self):
        self._sign_in()
        with self.assertRaises(VaultPasswordRequired):
            ensure_fresh(self.runtime)

    def test_skips_pull_when_fresh(self):
        self._sign_in()
        remember(self.runtime, 'secret')
        fresh = datetime.now(timezone.utc).isoformat()
        self.runtime.config.set('CloudSynchronization', 'last_synced', fresh)
        self.runtime.config.write()
        with patch('termius.sync.pull') as mocked:
            result = ensure_fresh(self.runtime)
        mocked.assert_not_called()
        self.assertFalse(result['pulled'])

    def test_pulls_when_stale(self):
        self._sign_in()
        remember(self.runtime, 'secret')
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        self.runtime.config.set('CloudSynchronization', 'last_synced', old)
        self.runtime.config.write()
        with patch('termius.sync.pull') as mocked:
            result = ensure_fresh(self.runtime)
        mocked.assert_called_once()
        self.assertTrue(result['pulled'])

    def test_ttl_zero_pulls_even_when_fresh(self):
        self._sign_in()
        remember(self.runtime, 'secret')
        fresh = datetime.now(timezone.utc).isoformat()
        self.runtime.config.set('CloudSynchronization', 'last_synced', fresh)
        self.runtime.config.write()
        with patch('termius.sync.pull') as mocked:
            result = ensure_fresh(self.runtime, ttl=0)
        mocked.assert_called_once()
        self.assertTrue(result['pulled'])

    def test_missing_last_synced_is_stale(self):
        self.assertTrue(is_stale(self.runtime.config))

    def test_ttl_env(self):
        os.environ['TERMIUS_SYNC_TTL'] = '15'
        self.assertEqual(sync_ttl(), 15)
        os.environ['TERMIUS_SYNC_TTL'] = 'nope'
        self.assertEqual(sync_ttl(), DEFAULT_SYNC_TTL)
