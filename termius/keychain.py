# -*- coding: utf-8 -*-
"""Keep credentials in the OS keychain through ``keyring``.

macOS uses the login Keychain, Windows the Credential Manager, and Linux
the Secret Service (GNOME Keyring, KWallet). ``PYTHON_KEYRING_BACKEND``
selects another backend when none of these is available.
"""
import os

import keyring
from cryptography.fernet import Fernet
from keyring.errors import PasswordDeleteError

STORAGE_KEY = 'storage_key'


class SecretStore(object):
    """Named secrets that belong to one Termius directory."""

    def __init__(self, directory_path):
        self.service = 'termius-mcp:{}'.format(
            os.path.abspath(str(directory_path))
        )

    def get(self, name):
        """Return the secret or None."""
        return keyring.get_password(self.service, name)

    def set(self, name, value):
        """Create or replace the secret."""
        keyring.set_password(self.service, name, value)

    def delete(self, name):
        """Delete the secret if it exists."""
        try:
            keyring.delete_password(self.service, name)
        except PasswordDeleteError:
            pass

    def storage_cipher(self):
        """Return the cipher for the local inventory; create its key once."""
        key = self.get(STORAGE_KEY)
        if not key:
            key = Fernet.generate_key().decode('ascii')
            self.set(STORAGE_KEY, key)
        return Fernet(key)
