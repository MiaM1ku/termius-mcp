# -*- coding: utf-8 -*-
"""Secret store selection and the encrypted local secrets file."""
import json
import logging
import multiprocessing
import os
import stat
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from termius import keychain
from termius.keychain import (
    FILE_FORMAT, FileSecretStore, KeyringSecretStore, STORAGE_KEY,
    create_secret_store,
)


def _write_named_key(directory, name, count):
    store = FileSecretStore(directory)
    for index in range(count):
        store.set(name, str(index))


def _publish_storage_key(directory, queue):
    store = FileSecretStore(directory)
    store.storage_cipher()
    queue.put(store.get(STORAGE_KEY))


def _fake_secretstorage(locked=False, missing=False):
    """A secretstorage stand-in that never creates a collection."""
    module = type('secretstorage', (), {})
    module.calls = []

    class _Connection(object):
        def close(self):
            pass

    class _Collection(object):
        def __init__(self, connection, path):
            self.connection = connection
            self.collection_path = path

        def is_locked(self):
            return locked

    def dbus_init():
        return _Connection()

    def collection(connection, path=keychain.DEFAULT_COLLECTION):
        if missing:
            raise RuntimeError('no such collection')
        return _Collection(connection, path)

    def get_default_collection(*_args, **_kwargs):
        module.calls.append('get_default_collection')
        raise AssertionError('must not create a collection')

    module.dbus_init = dbus_init
    module.Collection = collection
    module.get_default_collection = get_default_collection
    return module


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

    def test_another_machine_refuses_to_start(self):
        self.store.set('vault_password', 'hunter2')
        raw = self._raw()
        with patch.object(
            keychain, '_machine_material', return_value=b'another machine'
        ):
            reopened = FileSecretStore(self.tmpdir.name)
            with self.assertRaises(keychain.SecretStoreError) as caught:
                reopened.get('vault_password')
        self.assertIn('Re-enter the vault password', str(caught.exception))
        self.assertFalse(caught.exception.quarantine)
        self.assertEqual(self._bad_files(), [])
        self.assertEqual(self._raw(), raw)

    def test_runtime_refuses_a_machine_bound_file(self):
        self.store.set('User.apikey', 'device-token')
        raw = self._raw()
        previous = os.environ.get(keychain.KEYRING_ENV)
        os.environ[keychain.KEYRING_ENV] = '0'
        try:
            from termius.runtime import Runtime
            with patch.object(
                keychain, '_machine_material', return_value=b'another machine'
            ):
                with self.assertRaises(keychain.SecretStoreError):
                    Runtime(self.tmpdir.name)
            self.assertEqual(self._raw(), raw)
            self.assertEqual(self._bad_files(), [])
        finally:
            if previous is None:
                os.environ.pop(keychain.KEYRING_ENV, None)
            else:
                os.environ[keychain.KEYRING_ENV] = previous

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

    def test_missing_env_key_refuses_to_start(self):
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        self.store.set('User.apikey', 'KEY')
        raw = self._raw()
        os.environ.pop(keychain.SECRETS_KEY_ENV)
        reopened = FileSecretStore(self.tmpdir.name)
        with self.assertRaises(keychain.SecretStoreError) as caught:
            reopened.get('User.apikey')
        self.assertIn(keychain.SECRETS_KEY_ENV, str(caught.exception))
        self.assertFalse(caught.exception.quarantine)
        self.assertEqual(self._raw(), raw)
        self.assertEqual(self._bad_files(), [])
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        restored = FileSecretStore(self.tmpdir.name)
        self.assertEqual(restored.get('User.apikey'), 'KEY')

    def test_wrong_env_key_leaves_the_file_unchanged(self):
        os.environ[keychain.SECRETS_KEY_ENV] = 'container-secret'
        self.store.set('User.apikey', 'KEY')
        raw = self._raw()
        os.environ[keychain.SECRETS_KEY_ENV] = 'other-secret'
        with self.assertRaises(keychain.SecretStoreError) as caught:
            FileSecretStore(self.tmpdir.name).get('User.apikey')
        self.assertIn(keychain.SECRETS_KEY_ENV, str(caught.exception))
        self.assertEqual(self._raw(), raw)
        self.assertEqual(self._bad_files(), [])

    def test_other_version_is_quarantined(self):
        with open(self.store.path, 'w') as fileobj:
            fileobj.write(json.dumps({
                'format': 'termius-secrets/0',
                'kdf': 'machine',
                'salt': 'aaaa',
                'data': 'bbbb',
            }))
        self.assertIsNone(self.store.get('vault_password'))
        self.assertEqual(len(self._bad_files()), 1)
        self.assertFalse(os.path.exists(self.store.path))

    def test_writer_reloads_before_it_flushes(self):
        self.store.get('x')
        other = FileSecretStore(self.tmpdir.name)
        other.set('vault_password', 'NEW')
        other.set('User.apikey', 'KEY')
        self.assertEqual(self.store.get('vault_password'), 'NEW')
        self.store.set('storage_key', 'kA')
        saved = FileSecretStore(self.tmpdir.name)
        self.assertEqual(saved.get('vault_password'), 'NEW')
        self.assertEqual(saved.get('User.apikey'), 'KEY')
        self.assertEqual(saved.get('storage_key'), 'kA')

    def test_concurrent_writers_keep_every_key(self):
        names = ['k{}'.format(index) for index in range(8)]
        context = multiprocessing.get_context('fork')
        processes = [
            context.Process(
                target=_write_named_key,
                args=(self.tmpdir.name, name, 20),
            )
            for name in names
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        saved = FileSecretStore(self.tmpdir.name)
        for name in names:
            self.assertEqual(saved.get(name), '19')

    def test_concurrent_storage_cipher_shares_one_key(self):
        context = multiprocessing.get_context('fork')
        queue = context.Queue()
        processes = [
            context.Process(
                target=_publish_storage_key,
                args=(self.tmpdir.name, queue),
            )
            for _index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(queue.get(timeout=5), queue.get(timeout=5))

    def test_missing_machine_id_warns_to_set_the_env_key(self):
        with patch.object(keychain, '_machine_id', return_value=''):
            with self.assertLogs('termius.keychain', level='WARNING') as captured:
                FileSecretStore(self.tmpdir.name)
        self.assertEqual(len(captured.records), 1)
        self.assertIn(keychain.SECRETS_KEY_ENV, captured.records[0].getMessage())
        self.assertIsNone(captured.records[0].exc_info)

    def test_machine_id_does_not_use_the_mac_address(self):
        with patch.object(keychain, 'MACHINE_ID_PATHS', ()):
            with patch('uuid.getnode', side_effect=AssertionError('mac')):
                self.assertEqual(keychain._machine_id(), '')

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
    """The probe must use the default alias and must not create one."""

    def _usable(self, **kwargs):
        fake = _fake_secretstorage(**kwargs)
        with patch.dict('sys.modules', {'secretstorage': fake}):
            result = keychain.secret_service_usable()
        return result, fake.calls

    def test_locked_default_collection_is_not_usable(self):
        result, calls = self._usable(locked=True)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_missing_default_collection_is_not_usable(self):
        result, calls = self._usable(missing=True)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_unlocked_default_collection_is_usable(self):
        result, calls = self._usable(locked=False)
        self.assertTrue(result)
        self.assertEqual(calls, [])


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

    def test_locked_default_collection_saves_the_file_backend(self):
        fake = _fake_secretstorage(locked=True)
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.dict('sys.modules', {'secretstorage': fake}):
            store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, FileSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'file'
        )
        self.assertEqual(fake.calls, [])

    def test_auto_keyring_saves_backend_after_a_read(self):
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.object(
                    keychain, 'secret_service_usable', return_value=True
                ):
            store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, KeyringSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'keyring'
        )

    def test_auto_keyring_read_failure_saves_the_file(self):
        with patch.object(keychain, 'desktop_os', return_value=False), \
                patch.object(
                    keychain, 'secret_service_usable', return_value=True
                ), \
                patch.object(
                    KeyringSecretStore, 'get',
                    side_effect=RuntimeError('prompt dismissed'),
                ):
            store = create_secret_store(self.tmpdir.name)
        self.assertIsInstance(store, FileSecretStore)
        self.assertEqual(
            keychain.read_store_backend(self.tmpdir.name), 'file'
        )

    def test_keyring_copy_stops_after_the_first_error(self):
        os.environ[keychain.KEYRING_ENV] = '0'
        keyring_store = mock.Mock()
        keyring_store.get.side_effect = RuntimeError('locked')
        with self.assertLogs('termius.keychain', level='WARNING') as captured:
            keychain._copy_keyring_names(
                FileSecretStore(self.tmpdir.name), keyring_store
            )
        warnings = [
            record for record in captured.records
            if record.levelno >= logging.WARNING
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(warnings[0].exc_info)
        self.assertNotIn('Traceback', warnings[0].getMessage())
        self.assertEqual(keyring_store.get.call_count, 1)
