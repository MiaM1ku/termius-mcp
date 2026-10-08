# -*- coding: utf-8 -*-
from unittest import TestCase
import tempfile

import six
from unittest.mock import patch, Mock

from termius.core.models.base import Model
from termius.core.models.terminal import (
    Tag, SshKey, Identity, SshConfig, Group, Host, PFRule
)

from termius.core.storage import ApplicationStorage

class ModelsTest(TestCase):
    def test_generator(self):
        model_classes = (
            Host, Group, Tag,
            SshKey, Identity, SshConfig,
            PFRule
        )
        for model_class in model_classes:
            instance = model_class()
            not_fk_field_names = (
                k for k, v in instance.fields.items()
                if not issubclass(v.model, Model)
            )
            for i in not_fk_field_names:
                setattr(instance, i, i)

            self.save(instance)

    @patch('termius.core.storage.PersistentDict')
    def save(self, model, mocked):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        command = Mock()
        command.directory_path = tmp.name
        storage = ApplicationStorage(command)
        with storage:
            saved_model = storage.save(model)

        assert isinstance(saved_model.id, six.integer_types)

        fk_field_names = (
            k for k, v in saved_model.fields.items()
            if issubclass(v.model, Model)
        )
        for i in fk_field_names:
            setattr(model, i, None)
        stored_models = [saved_model]

        driver = mocked.return_value
        driver.__setitem__.assert_called_with(model.set_name, stored_models)
        driver.sync.assert_called_with()

    def test_allowed_fields(self):
        expected_allowed_fields = {
            Host: ['label', 'address', 'group', 'ssh_config',
                   'interaction_date', 'os_name', 'credentials_mode',
                   'id', 'remote_instance'],
            Group: ['label', 'ssh_config', 'parent_group', 'id',
                    'remote_instance', 'credentials_mode'],
            Tag: ['label', 'id', 'remote_instance'],
            SshKey: ['label', 'private_key', 'public_key', 'passphrase',
                     'id', 'remote_instance'],
            Identity: ['label', 'username', 'password', 'is_visible',
                       'ssh_key', 'id', 'remote_instance'],
            SshConfig: ['port', 'identity', 'startup_snippet',
                        'strict_host_key_check', 'use_ssh_key', 'timeout',
                        'keep_alive_packages', 'is_forward_ports', 'font_size',
                        'color_scheme', 'charset', 'cursor_blink',
                        'agent_forwarding', 'use_mosh', 'mosh_server_command',
                        'env_variables', 'id', 'remote_instance'],
            PFRule: ['label', 'host', 'pf_type', 'bound_address', 'local_port',
                     'hostname', 'remote_port', 'id', 'remote_instance']
        }

        for model_class, expected_fields in expected_allowed_fields.items():
            assert sorted(model_class.allowed_fields()) == sorted(expected_fields)
