# -*- coding: utf-8 -*-
"""Where the vault password, DeviceToken, and storage key are kept.

A desktop machine uses the OS keychain through ``keyring``: the macOS
login Keychain, the Windows Credential Manager, or the Linux Secret
Service (GNOME Keyring, KWallet). A machine without one — a server, a
container, a Linux box with no desktop session — keeps the same names in
``<directory>/secrets`` instead: one Fernet token, mode 0600.

The first start writes the choice to ``config`` as ``[Secrets] backend``.
Later starts keep that choice, so a desktop session and an SSH session on
the same machine do not split the login across two stores.
``TERMIUS_KEYRING=0`` forces the file, ``TERMIUS_KEYRING=1`` forces the
keychain, and either value replaces the saved choice.
``PYTHON_KEYRING_BACKEND`` selects the keychain only when no choice is
saved yet.

The file key comes from the machine id and the uid, unless
``TERMIUS_SECRETS_KEY`` is set. Set that variable in a container: a slim
image has no ``/etc/machine-id``, and this process does not fall back to
the MAC address.

A file with a broken format, or a file written by another version, is
renamed to ``secrets.bad-<UTC time>``. The process then starts with an
empty store. A file sealed with ``TERMIUS_SECRETS_KEY``, or bound to
another machine, is left unchanged and the process refuses to start.
"""
import base64
import json
import logging
import os
import stat
import sys
import tempfile
import time
from configparser import ConfigParser
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STORAGE_KEY = 'storage_key'
SECRETS_FILENAME = 'secrets'
KEYRING_ENV = 'TERMIUS_KEYRING'
KEYRING_BACKEND_ENV = 'PYTHON_KEYRING_BACKEND'
SECRETS_KEY_ENV = 'TERMIUS_SECRETS_KEY'
FILE_FORMAT = 'termius-secrets/1'
KDF_MACHINE = 'machine'
KDF_ENV = 'env'
STORE_SECTION = 'Secrets'
STORE_OPTION = 'backend'
BACKEND_FILE = 'file'
BACKEND_KEYRING = 'keyring'
MACHINE_ID_PATHS = ('/etc/machine-id', '/var/lib/dbus/machine-id')
# The transient collection GNOME Keyring always offers. Secrets written
# there are gone when the daemon exits.
SESSION_COLLECTION = '/org/freedesktop/secrets/collection/session'
# Alias keyring actually opens. Do not call get_default_collection: a
# missing alias makes that function create a collection and show a prompt.
DEFAULT_COLLECTION = '/org/freedesktop/secrets/aliases/default'
# Names written by the keychain-only releases. Copied once into an empty
# secrets file; the keychain entries are left in place.
LEGACY_KEYRING_NAMES = (
    'vault_password',
    'User.apikey',
    'User.private_key',
    'User.personal_v4_key',
    STORAGE_KEY,
)

logger = logging.getLogger(__name__)


class SecretStoreError(Exception):
    """The secrets file exists but this process cannot read it.

    ``quarantine`` is true only for a broken format or another version.
    A wrong key leaves the file in place and fails startup.
    """

    def __init__(self, message, quarantine=False):
        super(SecretStoreError, self).__init__(message)
        self.quarantine = quarantine


def _first_line(path):
    try:
        with open(path, 'r') as fileobj:
            return fileobj.read().strip()
    except (IOError, OSError):
        return ''


def _machine_id():
    """Return a stable host identifier, or '' when the host has none.

    There is no MAC-address fallback. A container without ``/etc/machine-id``
    must set ``TERMIUS_SECRETS_KEY``.
    """
    for path in MACHINE_ID_PATHS:
        value = _first_line(path)
        if value:
            return value
    return ''


def _machine_material():
    """Bytes that identify this machine and this user."""
    uid = os.getuid() if hasattr(os, 'getuid') else 0
    material = '{}\x1f{}\x1f{}'.format(
        sys.platform, uid, _machine_id()
    )
    return material.encode('utf-8')


def _derive_key(salt, material, info):
    """Derive a Fernet key from ``material``."""
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(material)
    return base64.urlsafe_b64encode(derived)


def _secrets_passphrase():
    """Return ``TERMIUS_SECRETS_KEY``, or '' when it is unset."""
    return os.environ.get(SECRETS_KEY_ENV, '').strip()


def _write_kdf():
    """KDF used for the next write of the secrets file."""
    if _secrets_passphrase():
        return KDF_ENV
    return KDF_MACHINE


def _key_for(kdf, salt):
    """Return the Fernet key for an envelope ``kdf`` value."""
    if kdf == KDF_MACHINE:
        return _derive_key(salt, _machine_material(), b'termius-mcp secrets')
    if kdf == KDF_ENV:
        passphrase = _secrets_passphrase()
        if not passphrase:
            raise SecretStoreError(
                '{} is not set.'.format(SECRETS_KEY_ENV),
                quarantine=False,
            )
        return _derive_key(
            salt, passphrase.encode('utf-8'), b'termius-mcp secrets env'
        )
    raise SecretStoreError(
        'Unknown secrets kdf {!r}.'.format(kdf), quarantine=True
    )


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

    def get_or_create(self, name, factory):
        """Return the secret, or store the value ``factory`` returns."""
        current = self.get(name)
        if current:
            return current
        created = factory()
        self.set(name, created)
        stored = self.get(name)
        return stored if stored else created

    def storage_cipher(self):
        """Return the local inventory cipher, creating its key once."""
        key = self.get_or_create(
            STORAGE_KEY, lambda: Fernet.generate_key().decode('ascii')
        )
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

    The file is bound to the machine and user that wrote it, unless it was
    sealed with ``TERMIUS_SECRETS_KEY``. A broken file is renamed aside.
    A wrong key or a different machine fails startup and leaves the file
    unchanged.

    ``set`` and ``delete`` take ``secrets.lock`` with ``flock``, re-read,
    and then write. ``get`` re-reads when the file stat changes, so one
    process cannot flush a stale cache over another process's keys.
    """

    def __init__(self, directory_path):
        self.path = os.path.join(str(directory_path), SECRETS_FILENAME)
        self._values = None
        self._stat = None
        if not _machine_id() and not _secrets_passphrase():
            logger.warning(
                'No machine id on this host and %s is unset; %s is only '
                'as private as the directory that holds it. Set %s.',
                SECRETS_KEY_ENV, self.path, SECRETS_KEY_ENV,
            )

    def _read_envelope(self, raw):
        try:
            envelope = json.loads(raw.decode('utf-8'))
            salt = base64.b64decode(envelope['salt'])
            token = envelope['data'].encode('ascii')
            marked_format = envelope['format']
            marked_kdf = envelope['kdf']
        except (ValueError, KeyError, TypeError):
            raise SecretStoreError(
                '{} is not a termius secrets file.'.format(self.path),
                quarantine=True,
            )
        known_kdf = marked_kdf in (KDF_MACHINE, KDF_ENV)
        if marked_format != FILE_FORMAT or not known_kdf:
            raise SecretStoreError(
                '{} was written by another version of termius-mcp.'.format(
                    self.path
                ),
                quarantine=True,
            )
        return salt, token, marked_kdf

    def _decrypt_payload(self, marked_kdf, salt, token):
        try:
            payload = Fernet(_key_for(marked_kdf, salt)).decrypt(token)
        except SecretStoreError:
            raise SecretStoreError(
                'Cannot decrypt {}. It was sealed with {}, and that '
                'variable is not set. Re-enter the passphrase in {} and '
                'start again. The file is unchanged.'.format(
                    self.path, SECRETS_KEY_ENV, SECRETS_KEY_ENV
                ),
                quarantine=False,
            )
        except InvalidToken:
            self._raise_bad_key(marked_kdf)
        return payload

    def _raise_bad_key(self, marked_kdf):
        if marked_kdf == KDF_ENV:
            raise SecretStoreError(
                'Cannot decrypt {}. {} does not match the passphrase '
                'that sealed it. Re-enter the passphrase and start '
                'again. The file is unchanged.'.format(
                    self.path, SECRETS_KEY_ENV
                ),
                quarantine=False,
            )
        raise SecretStoreError(
            'Cannot decrypt {}. The file is bound to the machine and '
            'the user that wrote it. This process will not start, and '
            'the file is unchanged. Move the file aside, start again, '
            'and sign in again. Re-enter the vault password.'.format(
                self.path
            ),
            quarantine=False,
        )

    def _decode(self, raw):
        salt, token, marked_kdf = self._read_envelope(raw)
        payload = self._decrypt_payload(marked_kdf, salt, token)
        try:
            values = json.loads(payload.decode('utf-8'))
        except ValueError:
            raise SecretStoreError(
                '{} does not hold a secrets map.'.format(self.path),
                quarantine=True,
            )
        if not isinstance(values, dict):
            raise SecretStoreError(
                '{} does not hold a secrets map.'.format(self.path),
                quarantine=True,
            )
        return values, marked_kdf

    def _quarantine(self, exc):
        """Rename an unreadable secrets file and keep it on disk."""
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        dest = '{}.bad-{}'.format(self.path, stamp)
        suffix = 0
        while os.path.exists(dest):
            suffix += 1
            dest = '{}.bad-{}-{}'.format(self.path, stamp, suffix)
        os.replace(self.path, dest)
        logger.warning(
            '%s Moved it to %s and started with an empty secret store. '
            'Sign in again. The previous file was kept.',
            exc, dest,
        )
        return dest

    def _read_raw(self):
        if not os.path.isfile(self.path):
            return None
        with open(self.path, 'rb') as fileobj:
            raw = fileobj.read()
        if not raw.strip():
            return None
        return raw

    def _file_stat(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))
        return (st.st_ino, mtime_ns, st.st_size)

    def _remember(self, values):
        self._values = values
        self._stat = self._file_stat()

    def _cache_fresh(self):
        return self._values is not None and self._stat == self._file_stat()

    @contextmanager
    def _locked(self):
        """Exclusive lock on ``secrets.lock``. No-op when ``flock`` is absent."""
        if fcntl is None:
            yield
            return
        directory = os.path.dirname(self.path) or '.'
        if not os.path.isdir(directory):
            os.makedirs(directory)
        lock_path = self.path + '.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.chmod(lock_path, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self):
        """Return the secret map. Caller must hold ``_locked`` for writes."""
        if self._cache_fresh():
            return self._values
        raw = self._read_raw()
        if raw is None:
            self._remember({})
            return self._values
        try:
            values, marked_kdf = self._decode(raw)
        except SecretStoreError as exc:
            if not exc.quarantine:
                self._values = None
                self._stat = None
                raise
            self._quarantine(exc)
            self._remember({})
            return self._values
        self._remember(values)
        if marked_kdf != _write_kdf():
            self._flush()
        return self._values

    def _flush(self):
        directory = os.path.dirname(self.path) or '.'
        if not os.path.isdir(directory):
            os.makedirs(directory)
        salt = os.urandom(16)
        kdf = _write_kdf()
        envelope = {
            'format': FILE_FORMAT,
            'kdf': kdf,
            'salt': base64.b64encode(salt).decode('ascii'),
            'data': Fernet(_key_for(kdf, salt)).encrypt(
                json.dumps(self._values).encode('utf-8')
            ).decode('ascii'),
        }
        handle, temp_path = tempfile.mkstemp(dir=directory, prefix='.secrets-')
        try:
            with os.fdopen(handle, 'wb') as fileobj:
                fileobj.write(json.dumps(envelope).encode('utf-8'))
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temp_path, self.path)
            self._stat = self._file_stat()
        except Exception:  # pylint: disable=broad-except
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def get(self, name):
        with self._locked():
            return self._load().get(name)

    def _drop_cache(self):
        self._values = None
        self._stat = None

    def set(self, name, value):
        with self._locked():
            self._drop_cache()
            self._load()[name] = value
            self._flush()

    def delete(self, name):
        with self._locked():
            self._drop_cache()
            values = self._load()
            if name in values:
                del values[name]
                self._flush()

    def get_or_create(self, name, factory):
        with self._locked():
            self._drop_cache()
            values = self._load()
            current = values.get(name)
            if current:
                return current
            created = factory()
            values[name] = created
            self._flush()
            return created


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


def _config_path(directory_path):
    return os.path.join(str(directory_path), 'config')


def read_store_backend(directory_path):
    """Return the saved backend, or None when ``config`` has no choice."""
    parser = ConfigParser()
    path = _config_path(directory_path)
    if os.path.isfile(path):
        parser.read(path)
    if not parser.has_option(STORE_SECTION, STORE_OPTION):
        return None
    value = parser.get(STORE_SECTION, STORE_OPTION).strip().lower()
    if value in (BACKEND_FILE, BACKEND_KEYRING):
        return value
    return None


def write_store_backend(directory_path, backend):
    """Record the secret-store choice without dropping other config."""
    directory = str(directory_path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    path = _config_path(directory)
    parser = ConfigParser()
    if os.path.isfile(path):
        parser.read(path)
    if parser.has_option(STORE_SECTION, STORE_OPTION):
        current = parser.get(STORE_SECTION, STORE_OPTION).strip().lower()
        if current == backend:
            return
    if not parser.has_section(STORE_SECTION):
        parser.add_section(STORE_SECTION)
    parser.set(STORE_SECTION, STORE_OPTION, backend)
    with open(path, 'w') as fileobj:
        parser.write(fileobj)


def secret_service_usable():
    """True when the default Secret Service collection is unlocked.

    The probe opens ``/org/freedesktop/secrets/aliases/default`` only.
    A missing alias or ``is_locked()`` true stays on the file store.
    This does not call ``get_default_collection``, which creates the
    collection and can show a prompt.
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
        # Construct the alias directly. get_default_collection creates the
        # collection, and that creation shows a prompt.
        collection = secretstorage.Collection(connection, DEFAULT_COLLECTION)
        return not collection.is_locked()
    except Exception:  # pylint: disable=broad-except
        return False
    finally:
        try:
            connection.close()
        except Exception:  # pylint: disable=broad-except
            pass


def _saved_uses_keyring(directory_path):
    """Return the saved choice, or None when ``config`` has none."""
    if directory_path is None:
        return None
    saved = read_store_backend(directory_path)
    if saved == BACKEND_KEYRING:
        return True
    if saved == BACKEND_FILE:
        return False
    return None


def use_keyring(directory_path=None):
    """Decide between the OS keychain and the encrypted local file.

    ``TERMIUS_KEYRING`` wins. Otherwise a choice already saved in
    ``config`` wins, so later starts do not switch stores. With no saved
    choice, ``PYTHON_KEYRING_BACKEND`` selects the keychain.
    """
    forced = keyring_setting()
    if forced is not None:
        return forced
    saved = _saved_uses_keyring(directory_path)
    if saved is not None:
        return saved
    if os.environ.get(KEYRING_BACKEND_ENV, '').strip():
        return True
    if desktop_os():
        return True
    return secret_service_usable()


def _copy_keyring_names(store, keyring_store):
    pending = []
    for name in LEGACY_KEYRING_NAMES:
        try:
            value = keyring_store.get(name)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                'Stopped copying OS keychain entries into %s after %s '
                'could not be read.',
                store.path, name,
            )
            return
        if value:
            pending.append((name, value))
    if not pending:
        return
    with store._locked():  # pylint: disable=protected-access
        values = store._load()  # pylint: disable=protected-access
        if values:
            return
        for name, value in pending:
            values[name] = value
        store._flush()  # pylint: disable=protected-access
    logger.warning(
        'Copied %s from the OS keychain into %s. The keychain entries '
        'were left in place.',
        ', '.join(name for name, _value in pending), store.path,
    )


def _import_keyring_secrets(directory_path, store):
    """Copy keychain entries into an empty file store. Leave the originals."""
    with store._locked():  # pylint: disable=protected-access
        if store._load():  # pylint: disable=protected-access
            return
    try:
        keyring_store = KeyringSecretStore(directory_path)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            'Cannot open the OS keychain to copy existing secrets.'
        )
        return
    _copy_keyring_names(store, keyring_store)


def _keyring_read_ok(store):
    """True when one ``storage_key`` read returns without an error."""
    try:
        store.get(STORAGE_KEY)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            'Cannot read %s from the OS keychain; keeping secrets in '
            '%s instead.',
            STORAGE_KEY, SECRETS_FILENAME,
        )
        return False
    return True


def create_secret_store(directory_path):
    """Return the secret store this machine can actually use."""
    forced = keyring_setting()
    if use_keyring(directory_path):
        try:
            store = KeyringSecretStore(directory_path)
        except ImportError:
            logger.warning(
                'keyring is not installed; keeping secrets in %s instead.',
                SECRETS_FILENAME,
            )
        else:
            saved = read_store_backend(directory_path)
            if forced is True or saved == BACKEND_KEYRING:
                if forced is True:
                    write_store_backend(directory_path, BACKEND_KEYRING)
                return store
            if _keyring_read_ok(store):
                write_store_backend(directory_path, BACKEND_KEYRING)
                return store
    store = FileSecretStore(directory_path)
    _import_keyring_secrets(directory_path, store)
    write_store_backend(directory_path, BACKEND_FILE)
    return store
