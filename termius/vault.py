# -*- coding: utf-8 -*-
"""Resolve and remember the Termius vault encryption password."""
import os

VAULT_ENV = 'TERMIUS_VAULT_PASSWORD'
VAULT_SECRET = 'vault_password'
LEGACY_VAULT_FILENAME = 'vault'


class VaultPasswordRequired(Exception):
    """No vault password in the environment or the secret store."""


def resolve(runtime):
    """Return the vault password or None.

    Order: ``TERMIUS_VAULT_PASSWORD``, then the secret store.
    """
    env = os.environ.get(VAULT_ENV)
    if env:
        return env
    return runtime.secrets.get(VAULT_SECRET) or None


def require(runtime):
    """Return the vault password or raise VaultPasswordRequired."""
    password = resolve(runtime)
    if not password:
        raise VaultPasswordRequired(
            'Vault password is not available. Call login or sync with '
            'remember=true, or set {}'.format(VAULT_ENV)
        )
    return password


def remember(runtime, password):
    """Store the password in the secret store."""
    if not password:
        raise VaultPasswordRequired('Cannot remember an empty vault password')
    runtime.secrets.set(VAULT_SECRET, password)


def forget(runtime):
    """Delete the remembered password if it exists."""
    runtime.secrets.delete(VAULT_SECRET)


def is_available(runtime):
    """True when env or the secret store can supply a password."""
    return resolve(runtime) is not None


def migrate_legacy_file(runtime):
    """Move the plaintext remember file of older versions to the store."""
    path = runtime.directory_path / LEGACY_VAULT_FILENAME
    if not path.is_file():
        return
    password = path.read_text()
    if password.endswith('\n'):
        password = password[:-1]
    if password:
        remember(runtime, password)
    path.unlink()
