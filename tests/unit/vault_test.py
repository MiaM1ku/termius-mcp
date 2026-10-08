# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

from termius.runtime import Runtime
from termius import vault


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runtime = Runtime(directory_path=self.tmpdir.name)
        self._env = os.environ.pop(vault.VAULT_ENV, None)

    def tearDown(self):
        vault.forget(self.runtime)
        if self._env is None:
            os.environ.pop(vault.VAULT_ENV, None)
        else:
            os.environ[vault.VAULT_ENV] = self._env
        self.tmpdir.cleanup()

    def test_env_wins_over_keychain(self):
        vault.remember(self.runtime, 'keychain-pass')
        os.environ[vault.VAULT_ENV] = 'env-pass'
        self.assertEqual(vault.resolve(self.runtime), 'env-pass')

    def test_forget_removes_password(self):
        vault.remember(self.runtime, 'secret')
        vault.forget(self.runtime)
        self.assertIsNone(vault.resolve(self.runtime))

    def test_require_raises_when_missing(self):
        with self.assertRaises(vault.VaultPasswordRequired):
            vault.require(self.runtime)

    def test_legacy_plaintext_moves_to_keychain(self):
        directory = self.runtime.directory_path
        (directory / 'vault').write_text('old-pass\n')
        (directory / 'config').write_text(
            '[User]\nusername = you@example.com\napikey = device-token\n'
        )
        (directory / 'ssh_keys').mkdir()
        (directory / 'ssh_keys' / 'id').write_text('PRIVATE')
        (directory / 'storage').write_text(
            json.dumps({'host_set': [{'id': 1, 'label': 'web'}]})
        )

        runtime = Runtime(directory_path=self.tmpdir.name)

        self.assertEqual(vault.resolve(runtime), 'old-pass')
        self.assertEqual(runtime.config.get('User', 'apikey'), 'device-token')
        self.assertFalse((directory / 'vault').exists())
        self.assertFalse((directory / 'ssh_keys').exists())
        on_disk = (directory / 'config').read_text()
        self.assertNotIn('device-token', on_disk)
        self.assertIn('you@example.com', on_disk)
        stored = (directory / 'storage').read_bytes()
        self.assertNotIn(b'web', stored)
        reopened = Runtime(directory_path=self.tmpdir.name)
        self.assertEqual(reopened.storage.driver['host_set'][0]['label'], 'web')
