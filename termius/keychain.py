# -*- coding: utf-8 -*-
"""Where the vault password, DeviceToken, and storage key are kept.

A desktop machine uses the OS keychain through ``keyring``: the macOS
login Keychain, the Windows Credential Manager, or the Linux Secret
Service (GNOME Keyring, KWallet). A machine without one — a server, a
container, a Linux box with no desktop session — keeps the same names in
``<directory>/secrets`` instead: one Fernet token, mode 0600, whose key
is derived from the machine identity. The file is unreadable text, and a
copy of it is useless on another machine or user. It does not hide
anything from somebody who is already this user on this machine.

``TERMIUS_KEYRING=0`` forces the file, ``TERMIUS_KEYRING=1`` forces the
keychain.
"""
import base64
import json
import logging
import os
import stat
import sys
import tempfile
import uuid

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STORAGE_KEY = 'storage_key'
SECRETS_FILENAME = 'secrets'
KEYRING_ENV = 'TERMIUS_KEYRING'
FILE_FORMAT = 'termius-secrets/1'
MACHINE_ID_PATHS = ('/etc/machine-id', '/var/lib/dbus/machine-id')
# The transient collection GNOME Keyring always offers. Secrets written
# there are gone when the daemon exits.
SESSION_COLLECTION = '/org/freedesktop/secrets/collection/session'

logger = logging.getLogger(__name__)


class SecretStoreError(Exception):
    """The secrets file exists but this process cannot read it."""


def _first_line(path):
    try:
        with open(path, 'r') as fileobj:
            return fileobj.read().strip()
    except (IOError, OSError):
        return ''


def _machine_id():
    """Return a stable host identifier, or '' when the host has none."""
    for path in MACHINE_ID_PATHS:
        value = _first_line(path)
        if value:
            return value
    node = uuid.getnode()
    if not node >> 40 & 0x01:  # The multicast bit marks a random node.
        return 'node:{:012x}'.format(node)
    return ''


def _machine_material():
    """Bytes that identify this machine and this user."""
    uid = os.getuid() if hasattr(os, 'getuid') else 0
    material = '{}\x1f{}\x1f{}'.format(
        sys.platform, uid, _machine_id()
    )
    return material.encode('utf-8')


def _file_key(salt):
    """Derive the Fernet key of the secrets file."""
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b'termius-mcp secrets',
    ).derive(_machine_material())
    return base64.urlsafe_b64encode(derived)


class SecretStore(object):
    """Named secrets plus the cipher of the encrypted local inventory."""

    def get(self, name):
        """Return the secret, or None."""
        raise NotImplementedError

    def set(self, name, value):
        """Create or replace the secret."""
        raise NotImplementedError

    def delete(self, name):
        """Delete the secret if it exists."""
        raise NotImplementedError

    def storage_cipher(self):
        """Return the local inventory cipher, creating its key once."""
        key = self.get(STORAGE_KEY)
        if not key:
            key = Fernet.generate_key().decode('ascii')
            self.set(STORAGE_KEY, key)
        return Fernet(key)


class KeyringSecretStore(SecretStore):
    """Secrets in the OS keychain, one item per name."""

    def __init__(self, directory_path):
        import keyring  # pylint: disable=import-outside-toplevel
        from keyring.errors import (  # pylint: disable=import-outside-toplevel
            PasswordDeleteError,
        )

        self._keyring = keyring
        self._delete_error = PasswordDeleteError
        self.service = 'termius-mcp:{}'.format(
            os.path.abspath(str(directory_path))
        )

    def get(self, name):
        return self._keyring.get_password(self.service, name)

    def set(self, name, value):
        self._keyring.set_password(self.service, name, value)

    def delete(self, name):
        try:
            self._keyring.delete_password(self.service, name)
        except self._delete_error:
            pass


class FileSecretStore(SecretStore):
    """Secrets in one encrypted file, mode 0600.

    The file is bound to the machine and user that wrote it, because the
    key is derived from both. Losing that binding — a new
    ``/etc/machine-id`` after a reinstall or a cloned image — makes the
    file unreadable, and the error says to delete it and sign in again.
    """

    def __init__(self, directory_path):
        self.path = os.path.join(str(directory_path), SECRETS_FILENAME)
        self._values = None
        if not _machine_id():
            logger.warning(
                'No machine id on this host; %s is only as private as '
                'the directory that holds it.', self.path
            )

    def _decode(self, raw):
        try:
            envelope = json.loads(raw.decode('utf-8'))
            salt = base64.b64decode(envelope['salt'])
            token = envelope['data'].encode('ascii')
            marked_format = envelope['format']
            marked_kdf = envelope['kdf']
        except (ValueError, KeyError, TypeError):
            raise SecretStoreError(
                '{} is not a termius secrets file. Delete it and sign in '
                'again.'.format(self.path)
            )
        if marked_format != FILE_FORMAT or marked_kdf != 'machine':
            raise SecretStoreError(
                '{} was written by another version of termius-mcp. Delete '
                'it and sign in again.'.format(self.path)
            )
        try:
            payload = Fernet(_file_key(salt)).decrypt(token)
        except InvalidToken:
            raise SecretStoreError(
                'Cannot decrypt {}. The file is bound to the machine and '
                'the user that wrote it. Delete it and sign in '
                'again.'.format(self.path)
            )
        try:
            values = json.loads(payload.decode('utf-8'))
        except ValueError:
            raise SecretStoreError(
                '{} does not hold a secrets map. Delete it and sign in '
                'again.'.format(self.path)
            )
        if not isinstance(values, dict):
            raise SecretStoreError(
                '{} does not hold a secrets map. Delete it and sign in '
                'again.'.format(self.path)
            )
        return values

    def _load(self):
        if self._values is not None:
            return self._values
        if not os.path.isfile(self.path):
            self._values = {}
            return self._values
        with open(self.path, 'rb') as fileobj:
            raw = fileobj.read()
        if not raw.strip():
            self._values = {}
            return self._values
        self._values = self._decode(raw)
        return self._values

    def _flush(self):
        directory = os.path.dirname(self.path) or '.'
        if not os.path.isdir(directory):
            os.makedirs(directory)
        salt = os.urandom(16)
        envelope = {
            'format': FILE_FORMAT,
            'kdf': 'machine',
            'salt': base64.b64encode(salt).decode('ascii'),
            'data': Fernet(_file_key(salt)).encrypt(
                json.dumps(self._values).encode('utf-8')
            ).decode('ascii'),
        }
        handle, temp_path = tempfile.mkstemp(dir=directory, prefix='.secrets-')
        try:
            with os.fdopen(handle, 'wb') as fileobj:
                fileobj.write(json.dumps(envelope).encode('utf-8'))
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temp_path, self.path)
        except Exception:  # pylint: disable=broad-except
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def get(self, name):
        return self._load().get(name)

    def set(self, name, value):
        self._load()[name] = value
        self._flush()

    def delete(self, name):
        values = self._load()
        if name in values:
            del values[name]
            self._flush()


def keyring_setting():
    """Read ``TERMIUS_KEYRING`` as True, False, or None when unset."""
    raw = os.environ.get(KEYRING_ENV, '').strip().lower()
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    return None


def desktop_os():
    """True on macOS and Windows, where the OS always has a keychain."""
    return sys.platform == 'darwin' or sys.platform.startswith('win')


def secret_service_usable():
    """True when the session has a Linux keychain that can store an item.

    A running Secret Service is not enough. GNOME Keyring always offers a
    transient ``session`` collection that keeps nothing after the daemon
    exits, and a daemon with no real collection asks for a password at a
    prompt that a headless session cannot show. Both cases stay on the
    file store.
    """
    try:
        import secretstorage  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    try:
        connection = secretstorage.dbus_init()
    except Exception:  # pylint: disable=broad-except
        return False
    try:
        collections = secretstorage.get_all_collections(connection)
        return any(
            item.collection_path != SESSION_COLLECTION
            for item in collections
        )
    except Exception:  # pylint: disable=broad-except
        return False
    finally:
        try:
            connection.close()
        except Exception:  # pylint: disable=broad-except
            pass


def use_keyring():
    """Decide between the OS keychain and the encrypted local file."""
    forced = keyring_setting()
    if forced is not None:
        return forced
    if desktop_os():
        return True
    return secret_service_usable()


def create_secret_store(directory_path):
    """Return the secret store this machine can actually use."""
    if use_keyring():
        try:
            return KeyringSecretStore(directory_path)
        except ImportError:
            logger.warning(
                'keyring is not installed; keeping secrets in %s instead.',
                SECRETS_FILENAME,
            )
    return FileSecretStore(directory_path)
