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
    create_secret_store,
)


class FileSecretStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._passphrase = os.environ.pop(keychain.SECRETS_KEY_ENV, None)
        self.store = FileSecretStore(self.tmpdir.name)

    def tearDown(self):
        if self._passphrase is None:
            os.environ.pop(keychain.SECRETS_KEY_ENV, None)
        else:
            os.environ[keychain.SECRETS_KEY_ENV] = self._passphrase
        self.tmpdir.cleanup()

    def _bad_files(self):
        return [
            name for name in os.listdir(self.tmpdir.name)
            if '.bad-' in name
        ]

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

    def test_another_machine_starts_empty_and_keeps_the_file(self):
        self.store.set('vault_password', 'hunter2')
        with patch.object(
            keychain, '_machine_material', return_value=b'another machine'
        ):
            reopened = FileSecretStore(self.tmpdir.name)
            self.assertIsNone(reopened.get('vault_password'))
        self.assertEqual(len(self._bad_files()), 1)
        self.assertFalse(os.path.exists(self.store.path))

    def test_plain_file_is_quarantined(self):
        with open(self.store.path, 'w') as fileobj:
            fileobj.write('vault_password=hunter2\n')
        self.assertIsNone(self.store.get('vault_password'))
        self.assertEqual(len(self._bad_files()), 1)
        self.assertFalse(os.path.exists(self.store.path))

    def test_env_key_opens_on_another_machine(self):
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        self.store.set('vault_password', 'hunter2')
        envelope = json.loads(self._raw().decode('utf-8'))
        self.assertEqual(envelope['kdf'], 'env')
        with patch.object(
            keychain, '_machine_material', return_value=b'another machine'
        ):
            reopened = FileSecretStore(self.tmpdir.name)
            self.assertEqual(reopened.get('vault_password'), 'hunter2')

    def test_setting_env_key_reseals_a_machine_file(self):
        self.store.set('vault_password', 'hunter2')
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        reopened = FileSecretStore(self.tmpdir.name)
        self.assertEqual(reopened.get('vault_password'), 'hunter2')
        envelope = json.loads(self._raw().decode('utf-8'))
        self.assertEqual(envelope['kdf'], 'env')

    def test_missing_env_key_quarantines_the_file(self):
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        self.store.set('vault_password', 'hunter2')
        os.environ.pop(keychain.SECRETS_KEY_ENV)
        reopened = FileSecretStore(self.tmpdir.name)
        self.assertIsNone(reopened.get('vault_password'))
        self.assertEqual(len(self._bad_files()), 1)

    def test_runtime_starts_when_the_secrets_file_is_unreadable(self):
        with open(self.store.path, 'w') as fileobj:
            fileobj.write('not-a-secrets-file')
        previous = os.environ.get(keychain.KEYRING_ENV)
        os.environ[keychain.KEYRING_ENV] = '0'
        try:
            from termius.runtime import Runtime
            runtime = Runtime(self.tmpdir.name)
            self.assertIsNone(runtime.secrets.get('vault_password'))
            self.assertFalse(runtime.config.get_safe('User', 'apikey'))
            self.assertEqual(len(self._bad_files()), 1)
        finally:
            if previous is None:
                os.environ.pop(keychain.KEYRING_ENV, None)
            else:
                os.environ[keychain.KEYRING_ENV] = previous

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
        self._backend = os.environ.pop(keychain.KEYRING_BACKEND_ENV, None)
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()
        if self._forced is None:
            os.environ.pop(keychain.KEYRING_ENV, None)
        else:
            os.environ[keychain.KEYRING_ENV] = self._forced
        if self._backend is None:
            os.environ.pop(keychain.KEYRING_BACKEND_ENV, None)
        else:
            os.environ[keychain.KEYRING_BACKEND_ENV] = self._backend

    def test_env_forces_the_file(self):
        os.environ[keychain.KEYRING_ENV] = '0'
        self.assertFalse(keychain.use_keyring())
        store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, FileSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'file'
        )

    def test_env_forces_the_keychain(self):
        os.environ[keychain.KEYRING_ENV] = '1'
        self.assertTrue(keychain.use_keyring())
        store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, KeyringSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'keyring'
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
                create_secret_store(self.tmpdir.name), FileSecretStore
            )
            self.assertEqual(
                keychain.read_store_backend(self.tmpdir.name), 'file'
            )

    def test_keychain_falls_back_when_keyring_is_missing(self):
        with patch.object(keychain, 'use_keyring', return_value=True), \
                patch.object(
                    keychain.KeyringSecretStore,
                    '__init__',
                    side_effect=ImportError('no keyring'),
                ):
            self.assertIsInstance(
                create_secret_store(self.tmpdir.name), FileSecretStore
            )

    def test_python_keyring_backend_selects_the_keychain(self):
        os.environ[keychain.KEYRING_BACKEND_ENV] = (
            'keyring.backends.null.Keyring'
        )
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.object(
                    keychain, 'secret_service_usable', return_value=False
                ):
            self.assertTrue(keychain.use_keyring(self.tmpdir.name))

    def test_saved_file_choice_ignores_a_later_desktop(self):
        keychain.write_store_backend(self.tmpdir.name, 'file')
        with patch.object(keychain, 'desktop_os', return_value=True):
            store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, FileSecretStore)

    def test_saved_keyring_choice_ignores_a_later_headless_probe(self):
        keychain.write_store_backend(self.tmpdir.name, 'keyring')
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.object(
                    keychain, 'secret_service_usable', return_value=False
                ):
            store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, KeyringSecretStore)

    def test_termius_keyring_overrides_the_saved_choice(self):
        keychain.write_store_backend(self.tmpdir.name, 'keyring')
        os.environ[keychain.KEYRING_ENV] = '0'
        store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, FileSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'file'
        )

    def test_empty_file_store_copies_keyring_entries(self):
        os.environ[keychain.KEYRING_ENV] = '1'
        keyring_store = create_secret_store(self.tmpdir.name)
        keyring_store.set('vault_password', 'hunter2')
        keyring_store.set('User.apikey', 'device-token')
        keyring_store.set(STORAGE_KEY, 'storage-key')
        os.environ[keychain.KEYRING_ENV] = '0'
        file_store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(file_store, FileSecretStore)
        self.assertEqual(file_store.get('vault_password'), 'hunter2')
        self.assertEqual(file_store.get('User.apikey'), 'device-token')
        self.assertEqual(file_store.get(STORAGE_KEY), 'storage-key')
        self.assertEqual(keyring_store.get('vault_password'), 'hunter2')

    def test_nonempty_file_store_does_not_copy_keyring_entries(self):
        os.environ[keychain.KEYRING_ENV] = '1'
        keyring_store = create_secret_store(self.tmpdir.name)
        keyring_store.set('vault_password', 'from-keyring')
        os.environ[keychain.KEYRING_ENV] = '0'
        FileSecretStore(self.tmpdir.name).set('vault_password', 'from-file')
        file_store = create_secret_store(self.tmpdir.name)
        self.assertEqual(file_store.get('vault_password'), 'from-file')
