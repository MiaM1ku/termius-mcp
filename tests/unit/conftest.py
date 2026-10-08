# -*- coding: utf-8 -*-
"""Keep tests out of the real OS keychain."""
import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super(MemoryKeyring, self).__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self.items[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username)


keyring.set_keyring(MemoryKeyring())
