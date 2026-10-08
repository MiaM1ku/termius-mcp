# -*- coding: utf-8 -*-
"""Module for keeping application config."""
from pathlib2 import Path
from six import PY2
from six.moves import configparser

from .paths import directory_of

# Options kept in the secret store instead of the config file.
SECRET_OPTIONS = frozenset((
    ('User', 'apikey'),
    ('User', 'private_key'),
    ('User', 'personal_v4_key'),
))


class Config(object):
    """Class for application config."""

    paths = ['{application_directory}/config']
    write_mode = 'wb' if PY2 else 'w'

    def __init__(self, app, secrets=None, **kwargs):
        """Create new config.

        ``app`` is a Runtime (or test double) with ``directory_path``.
        ``secrets`` is a secret store for ``SECRET_OPTIONS``; without it
        they stay in the config file.
        """
        assert self.paths, "It must have at least single config file's path."
        paths_kwargs = dict(
            application_directory=directory_of(app), **kwargs
        )
        self._paths = [Path(i.format(**paths_kwargs)) for i in self.paths]
        self.touch_files()
        self.config = configparser.ConfigParser()
        self.config.read([str(i) for i in self._paths])
        self.app = app
        self.command = app
        self.secrets = secrets
        self._move_secrets_to_store()

    def _secret_name(self, section, option):
        if self.secrets is None or (section, option) not in SECRET_OPTIONS:
            return None
        return '{}.{}'.format(section, option)

    def _move_secrets_to_store(self):
        """Migrate plaintext secrets written by older versions."""
        moved = False
        for section, option in SECRET_OPTIONS:
            name = self._secret_name(section, option)
            if name and self.config.has_option(section, option):
                self.secrets.set(name, self.config.get(section, option))
                self.config.remove_option(section, option)
                moved = True
        if moved:
            self.write()

    @property
    def user_config_path(self):
        """Return particular user config path."""
        return self._paths[-1]

    def touch_files(self):
        """Touch config file paths."""
        for i in self._paths:
            if not i.is_file():
                i.touch()

    def get(self, section, option):
        """Get option value from config."""
        name = self._secret_name(section, option)
        if name is None:
            return self.config.get(section, option)
        value = self.secrets.get(name)
        if value is None:
            raise configparser.NoOptionError(option, section)
        return value

    def get_safe(self, section, option, default=None):
        """Get option value from config."""
        try:
            return self.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def set(self, section, option, value):
        """Set option value to config. Secrets are stored immediately."""
        name = self._secret_name(section, option)
        if name is not None:
            self.secrets.set(name, value)
            return
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, option, value)

    def remove(self, section, option):
        """Remove option value from config."""
        name = self._secret_name(section, option)
        if name is not None:
            self.secrets.delete(name)
        elif self.config.has_section(section):
            self.config.remove_option(section, option)

    def remove_section(self, section):
        """Remove section and all options from config."""
        for secret_section, option in SECRET_OPTIONS:
            if secret_section == section:
                self.remove(section, option)
        if self.config.has_section(section):
            self.config.remove_section(section)

    def write(self):
        """Write config for current user config file."""
        with self.user_config_path.open(self.write_mode) as _file:
            self.config.write(_file)
