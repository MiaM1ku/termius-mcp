# -*- coding: utf-8 -*-
import tempfile
import unittest
from unittest.mock import patch

from termius.core.models.terminal import Host, Identity, SshConfig
from termius.mcp.server import handle_rpc
from termius.core.ssh_exec import SshExecError
from termius.core.ssh_files import SshFileError
from termius.mcp.tools import ToolError, call_tool
from termius.runtime import Runtime


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runtime = Runtime(directory_path=self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _sign_in(self):
        self.runtime.config.set('User', 'username', 'you@example.com')
        self.runtime.config.set('User', 'apikey', 'token')
        self.runtime.config.set('User', 'salt', 'c2FsdA==')
        self.runtime.config.set('User', 'hmac_salt', 'aG1hYw==')
        self.runtime.config.write()

    def _add_host(self, label='web', address='10.0.0.1', username='root'):
        identity = Identity(label=label, username=username, is_visible=True)
        with self.runtime.storage:
            saved_identity = self.runtime.storage.save(identity)
            ssh_config = SshConfig(port=22, identity=saved_identity.id)
            saved_config = self.runtime.storage.save(ssh_config)
            host = Host(
                label=label, address=address, ssh_config=saved_config.id
            )
            return self.runtime.storage.save(host)

    def test_status_not_signed_in(self):
        data, summary = call_tool(self.runtime, 'status', {})
        self.assertFalse(data['logged_in'])
        self.assertIn('Not signed in', summary)

    def test_hosts_requires_login(self):
        with self.assertRaises(ToolError) as caught:
            call_tool(self.runtime, 'hosts', {})
        self.assertEqual(caught.exception.code, 'not_signed_in')

    def test_hosts_requires_vault_password(self):
        self._sign_in()
        with self.assertRaises(ToolError) as caught:
            call_tool(self.runtime, 'hosts', {})
        self.assertEqual(caught.exception.code, 'vault_password_required')

    def test_host_not_found(self):
        self._sign_in()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with self.assertRaises(ToolError) as caught:
                call_tool(self.runtime, 'host', {'name': 'missing'})
        self.assertEqual(caught.exception.code, 'host_not_found')

    def test_host_ambiguous(self):
        self._sign_in()
        self._add_host('dup', '1.1.1.1')
        self._add_host('dup', '2.2.2.2')
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with self.assertRaises(ToolError) as caught:
                call_tool(self.runtime, 'host', {'name': 'dup'})
        self.assertEqual(caught.exception.code, 'host_not_found')
        self.assertIn('Multiple hosts', str(caught.exception))

    def test_hosts_and_host_shape(self):
        self._sign_in()
        saved = self._add_host()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            listing, _ = call_tool(self.runtime, 'hosts', {'query': 'web'})
            detail, _ = call_tool(self.runtime, 'host', {'name': saved.id})
        self.assertEqual(listing['count'], 1)
        self.assertEqual(listing['hosts'][0]['address'], '10.0.0.1')
        self.assertEqual(detail['username'], 'root')
        self.assertIn('ssh_command', detail)
        self.assertTrue(detail['ssh_command'].startswith('ssh'))
        self.assertNotIn('password', detail)

    def test_exec_shape(self):
        self._sign_in()
        saved = self._add_host()
        fake = {
            'host': 'web',
            'address': '10.0.0.1',
            'username': 'root',
            'command': 'uname',
            'exit_code': 0,
            'stdout': 'Linux\n',
            'stderr': '',
            'truncated': False,
        }
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with patch(
                'termius.mcp.tools.run_host_command', return_value=dict(fake)
            ):
                data, summary = call_tool(
                    self.runtime, 'exec',
                    {'name': saved.label, 'command': 'uname'},
                )
        self.assertTrue(data['ok'])
        self.assertEqual(data['exit_code'], 0)
        self.assertEqual(data['stdout'], 'Linux\n')
        self.assertIn('exit 0', summary)

    def test_files_shape(self):
        self._sign_in()
        saved = self._add_host()
        fake = {
            'host': 'web',
            'address': '10.0.0.1',
            'username': 'root',
            'action': 'list',
            'path': '/home/root',
            'entries': [{'name': 'a', 'type': 'file', 'size': 1}],
            'count': 1,
            'ok': True,
        }
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with patch(
                'termius.mcp.tools.run_file_action', return_value=dict(fake)
            ):
                data, summary = call_tool(
                    self.runtime, 'files',
                    {'name': saved.label, 'action': 'list'},
                )
        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 1)
        self.assertIn('1 entries', summary)

    def test_files_rejects_bad_action(self):
        self._sign_in()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with self.assertRaises(ToolError) as caught:
                call_tool(
                    self.runtime, 'files',
                    {'name': 'web', 'action': 'chmod'},
                )
        self.assertEqual(caught.exception.code, 'invalid_argument')

    def test_files_requires_path(self):
        self._sign_in()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with self.assertRaises(ToolError) as caught:
                call_tool(
                    self.runtime, 'files',
                    {'name': 'web', 'action': 'read'},
                )
        self.assertEqual(caught.exception.code, 'invalid_argument')

    def test_files_ssh_error(self):
        self._sign_in()
        saved = self._add_host()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with patch(
                'termius.mcp.tools.run_file_action',
                side_effect=SshFileError('permission denied'),
            ):
                with self.assertRaises(ToolError) as caught:
                    call_tool(
                        self.runtime, 'files',
                        {
                            'name': saved.label,
                            'action': 'read',
                            'path': '/etc/shadow',
                        },
                    )
        self.assertEqual(caught.exception.code, 'file_failed')

    def test_files_connect_error(self):
        self._sign_in()
        saved = self._add_host()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with patch(
                'termius.mcp.tools.run_file_action',
                side_effect=SshExecError('SSH to 10.0.0.1 failed: timeout'),
            ):
                with self.assertRaises(ToolError) as caught:
                    call_tool(
                        self.runtime, 'files',
                        {'name': saved.label, 'action': 'list'},
                    )
        self.assertEqual(caught.exception.code, 'ssh_failed')

    def test_files_requires_login(self):
        with self.assertRaises(ToolError) as caught:
            call_tool(self.runtime, 'files', {'action': 'list'})
        self.assertEqual(caught.exception.code, 'not_signed_in')

    def test_inventory_rejects_bad_kind(self):
        self._sign_in()
        with patch('termius.mcp.tools.ensure_fresh', return_value={}):
            with self.assertRaises(ToolError) as caught:
                call_tool(self.runtime, 'inventory', {'kind': 'tags'})
        self.assertEqual(caught.exception.code, 'invalid_argument')

    def test_unknown_tool(self):
        with self.assertRaises(ToolError) as caught:
            call_tool(self.runtime, 'push', {})
        self.assertEqual(caught.exception.code, 'unknown_tool')

    def test_login_google_start(self):
        data, summary = call_tool(
            self.runtime, 'login', {'method': 'google'}
        )
        self.assertEqual(data['next'], 'login_complete')
        self.assertIn('account.termius.com', data['url'])
        self.assertIn('login_complete', summary)

    def test_login_email_requires_password(self):
        with self.assertRaises(ToolError) as caught:
            call_tool(
                self.runtime, 'login',
                {'method': 'email', 'username': 'you@example.com'},
            )
        self.assertEqual(caught.exception.code, 'login_failed')

    def test_handle_rpc_tool_error(self):
        response = handle_rpc(self.runtime, {
            'jsonrpc': '2.0',
            'id': 7,
            'method': 'tools/call',
            'params': {'name': 'hosts', 'arguments': {}},
        })
        self.assertTrue(response['result']['isError'])
        self.assertEqual(response['id'], 7)

    def test_handle_rpc_initialize(self):
        response = handle_rpc(self.runtime, {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {},
        })
        self.assertEqual(
            response['result']['serverInfo']['name'], 'termius'
        )
        self.assertEqual(
            response['result']['serverInfo']['title'], 'Termius Cloud'
        )
        self.assertEqual(response['result']['serverInfo']['version'], '3.0.0')
        self.assertEqual(
            response['result']['protocolVersion'], '2025-06-18'
        )

    def test_tools_list_has_ten(self):
        response = handle_rpc(self.runtime, {
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/list',
        })
        tools = response['result']['tools']
        names = [tool['name'] for tool in tools]
        self.assertEqual(
            names,
            [
                'status', 'login', 'login_complete', 'logout', 'sync',
                'hosts', 'host', 'exec', 'files', 'inventory',
            ],
        )
        for tool in tools:
            self.assertTrue(tool.get('title'), tool['name'])
            self.assertTrue(tool.get('description'), tool['name'])
            self.assertEqual(tool['inputSchema']['type'], 'object')
            self.assertEqual(tool['annotations']['title'], tool['title'])
