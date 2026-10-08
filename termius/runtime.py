# -*- coding: utf-8 -*-
"""Process-wide Termius paths, config, and local storage."""
from os.path import expanduser
import shutil

from pathlib2 import Path

from .core.settings import Config
from .core.signals import post_logout
from .core.storage import ApplicationStorage
from .core.storage.strategies import RelatedGetStrategy, SyncSaveStrategy
from .core.subscribers import clean_data
from .keychain import SecretStore
from .vault import migrate_legacy_file


class Runtime(object):
    """Application context used by MCP tools. Not a CLI app."""

    def __init__(self, directory_path=None):
        if directory_path is None:
            directory_path = expanduser('~/.termius/')
        self.directory_path = Path(directory_path)
        if not self.directory_path.is_dir():
            self.directory_path.mkdir(parents=True)
        post_logout.connect(clean_data)
        self.secrets = SecretStore(self.directory_path)
        self.config = Config(self, secrets=self.secrets)
        self._storage_cipher = self.secrets.storage_cipher()
        self.storage = self._open_storage()
        self._remove_legacy_plaintext()

    def reload_storage(self):
        """Re-open storage after another writer flushed the file."""
        self.storage = self._open_storage()

    def _open_storage(self):
        return ApplicationStorage(
            self,
            get_strategy=RelatedGetStrategy,
            save_strategy=SyncSaveStrategy,
            cipher=self._storage_cipher,
        )

    def _remove_legacy_plaintext(self):
        """Clean up plaintext secrets that older versions wrote to disk."""
        migrate_legacy_file(self)
        ssh_keys = self.directory_path / 'ssh_keys'
        if ssh_keys.is_dir():
            shutil.rmtree(str(ssh_keys))
        if self.config.config.has_section('SSH_keys'):
            self.config.remove_section('SSH_keys')
            self.config.write()
