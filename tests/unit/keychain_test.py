# -*- coding: utf-8 -*-
"""Secret store selection and the encrypted local secrets file."""
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from termius import keychain
from termius.keychain import (
    FILE_FORMAT, FileSecretStore, KeyringSecretStore, STORAGE_KEY,
    SecretStoreError, create_secret_store,
)


class FileSecretStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = FileSecretStore(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _raw(self):
        with open(self.store.path, 'rb') as fileobj:
            return fileobj.read()

    def test_roundtrip(self):
        self.store.set('vault_password', 'hunter2')
        self.assertEqual(self.store.get('vault_password'), 'hunter2')
        self.assertIsNone(self.store.get('missing'))

    def test_reopen_keeps_values(self):
        self.store.set('User.apikey', 'device-token')
        reopened = FileSecretStore(self.tmpdir.name)
        self.assertEqual(reopened.get('User.apikey'), 'device-token')

    def test_delete(self):
        self.store.set('vault_password', 'hunter2')
        self.store.delete('vault_password')
        self.assertIsNone(self.store.get('vault_password'))

    def test_file_holds_no_plaintext(self):
        self.store.set('vault_password', 'hunter2')
        self.assertNotIn(b'hunter2', self._raw())

    def test_missing_file_reads_as_empty(self):
        self.assertIsNone(self.store.get('vault_password'))
        self.assertFalse(os.path.exists(self.store.path))

    def test_empty_file_reads_as_empty(self):
        open(self.store.path, 'wb').close()
        self.assertIsNone(self.store.get('vault_password'))

    def test_file_mode_is_0600(self):
        self.store.set('vault_password', 'hunter2')
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_envelope_is_self_describing(self):
        self.store.set('vault_password', 'hunter2')
        envelope = json.loads(self._raw().decode('utf-8'))
        self.assertEqual(envelope['format'], FILE_FORMAT)
        self.assertEqual(envelope['kdf'], 'machine')
        self.assertTrue(envelope['salt'])
        self.assertTrue(envelope['data'])

    def test_another_machine_cannot_read_it(self):
        self.store.set('vault_password', 'hunter2')
        with patch.object(
            keychain, '_machine_material', return_value=b'another machine'
        ):
            reopened = FileSecretStore(self.tmpdir.name)
            with self.assertRaises(SecretStoreError) as caught:
                reopened.get('vault_password')
        self.assertIn('Delete it', str(caught.exception))

    def test_plain_file_is_rejected(self):
        with open(self.store.path, 'w') as fileobj:
            fileobj.write('vault_password=hunter2\n')
        with self.assertRaises(SecretStoreError):
            self.store.get('vault_password')

    def test_storage_cipher_survives_a_reopen(self):
        token = self.store.storage_cipher().encrypt(b'payload')
        reopened = FileSecretStore(self.tmpdir.name)
        self.assertEqual(
            reopened.storage_cipher().decrypt(token), b'payload'
        )

    def test_storage_cipher_is_stored_by_name(self):
        self.store.storage_cipher()
        self.assertTrue(self.store.get(STORAGE_KEY))


class KeyringSecretStoreTest(unittest.TestCase):
    """The test session pins an in-memory keyring (tests/unit/conftest.py)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = KeyringSecretStore(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_service_name_carries_the_directory(self):
        self.assertTrue(self.store.service.startswith('termius-mcp:'))
        self.assertIn(os.path.abspath(self.tmpdir.name), self.store.service)

    def test_roundtrip_and_delete(self):
        self.store.set('vault_password', 'hunter2')
        self.assertEqual(self.store.get('vault_password'), 'hunter2')
        self.store.delete('vault_password')
        self.assertIsNone(self.store.get('vault_password'))

    def test_delete_missing_name_is_quiet(self):
        self.store.delete('never-stored')


class SecretServiceProbeTest(unittest.TestCase):
    """The probe must reject a transient-only Secret Service."""

    class _Collection(object):
        def __init__(self, path):
            self.collection_path = path

    class _Connection(object):
        def close(self):
            pass

    def _fake(self, paths):
        connection = self._Connection()
        module = type('secretstorage', (), {})
        module.dbus_init = lambda: connection
        module.get_all_collections = lambda conn: [
            self._Collection(path) for path in paths
        ]
        return module

    def _probe(self, paths):
        with patch.dict('sys.modules', {'secretstorage': self._fake(paths)}):
            return keychain.secret_service_usable()

    def test_session_collection_alone_is_not_enough(self):
        self.assertFalse(self._probe([keychain.SESSION_COLLECTION]))

    def test_persistent_collection_counts(self):
        self.assertTrue(self._probe([
            keychain.SESSION_COLLECTION,
            '/org/freedesktop/secrets/collection/login',
        ]))

    def test_no_collection_is_not_usable(self):
        self.assertFalse(self._probe([]))


class StoreSelectionTest(unittest.TestCase):
    def setUp(self):
        self._forced = os.environ.pop(keychain.KEYRING_ENV, None)

    def tearDown(self):
        if self._forced is None:
            os.environ.pop(keychain.KEYRING_ENV, None)
        else:
            os.environ[keychain.KEYRING_ENV] = self._forced

    def test_env_forces_the_file(self):
        os.environ[keychain.KEYRING_ENV] = '0'
        self.assertFalse(keychain.use_keyring())
        self.assertIsInstance(create_secret_store('/tmp'), FileSecretStore)

    def test_env_forces_the_keychain(self):
        os.environ[keychain.KEYRING_ENV] = '1'
        self.assertTrue(keychain.use_keyring())
        self.assertIsInstance(
            create_secret_store('/tmp'), KeyringSecretStore
        )

    def test_desktop_defaults_to_the_keychain(self):
        with patch.object(keychain, 'desktop_os', return_value=True):
            self.assertTrue(keychain.use_keyring())

    def test_headless_defaults_to_the_file(self):
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.object(
                    keychain, 'secret_service_usable', return_value=False
                ):
            self.assertFalse(keychain.use_keyring())
            self.assertIsInstance(
                create_secret_store('/tmp'), FileSecretStore
            )

    def test_keychain_falls_back_when_keyring_is_missing(self):
        with patch.object(keychain, 'use_keyring', return_value=True), \
                patch.object(
                    keychain.KeyringSecretStore,
                    '__init__',
                    side_effect=ImportError('no keyring'),
                ):
            self.assertIsInstance(
                create_secret_store('/tmp'), FileSecretStore
            )
