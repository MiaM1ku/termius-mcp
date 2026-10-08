# -*- coding: utf-8 -*-
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from termius.cli import build_parser, run
from termius.core.exceptions import ApiError
from termius.main import main
from termius.runtime import Runtime
from termius.vault import VAULT_ENV


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runtime = Runtime(directory_path=self.tmpdir.name)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._vault = os.environ.pop(VAULT_ENV, None)

    def tearDown(self):
        if self._vault is None:
            os.environ.pop(VAULT_ENV, None)
        else:
            os.environ[VAULT_ENV] = self._vault
        self.tmpdir.cleanup()

    def _run(self, argv, prompt=None, secret=None, isatty=True):
        return run(
            argv,
            runtime=self.runtime,
            prompt=prompt or self._unused_prompt,
            secret=secret or self._unused_secret,
            stdout=self.stdout,
            stderr=self.stderr,
            isatty=isatty,
        )

    def _unused_prompt(self, message):
        raise AssertionError('unexpected prompt: {}'.format(message))

    def _unused_secret(self, message):
        raise AssertionError('unexpected secret: {}'.format(message))

    def test_parser_lists_login_methods(self):
        parser = build_parser()
        self.assertIn('login', parser.format_help())
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            with self.assertRaises(SystemExit) as caught:
                parser.parse_args(['login', '--help'])
        self.assertEqual(caught.exception.code, 0)
        login_text = stdout.getvalue()
        self.assertIn('google', login_text)
        self.assertIn('email', login_text)

    def test_missing_command_prints_help(self):
        code = self._run([], isatty=False)
        self.assertEqual(code, 2)
        self.assertIn('login', self.stderr.getvalue())

    @patch('termius.cli.login_google_complete')
    @patch('termius.cli.login_google_start')
    def test_login_google(self, start, complete):
        start.return_value = {
            'url': (
                'https://account.termius.com/sso/desktop'
                '?provider=google&request=abc'
            ),
        }
        complete.return_value = {'ok': True, 'username': 'you@example.com'}
        callback = (
            'termius://app/continue-sso?email=you@example.com'
            '&firebaseToken=tok&requestId=abc'
        )
        code = self._run(
            ['login', 'google'],
            prompt=lambda message: callback,
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 0)
        self.assertIn('account.termius.com', self.stderr.getvalue())
        self.assertIn('Signed in as you@example.com', self.stdout.getvalue())
        complete.assert_called_once_with(
            self.runtime, callback, 'vault-pass',
            otp=None, remember_password=True,
        )

    @patch('termius.cli.login_google_complete')
    @patch('termius.cli.login_google_start')
    def test_login_google_otp_prompt(self, start, complete):
        start.return_value = {
            'url': 'https://account.termius.com/sso/desktop?request=abc',
        }
        complete.side_effect = [
            ValueError(
                'This account requires otp. Call login_complete again with otp.'
            ),
            {'ok': True, 'username': 'you@example.com'},
        ]
        answers = iter(['termius://app/continue-sso?x=1', '654321'])
        code = self._run(
            ['login', 'google'],
            prompt=lambda message: next(answers),
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 0)
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(complete.call_args[1]['otp'], '654321')

    @patch('termius.cli.login_email')
    def test_login_email(self, login_email):
        login_email.return_value = {'ok': True, 'username': 'you@example.com'}
        code = self._run(
            ['login', 'email', '-u', 'you@example.com', '--no-remember'],
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 0)
        self.assertIn('Signed in as you@example.com', self.stdout.getvalue())
        login_email.assert_called_once_with(
            self.runtime, 'you@example.com', 'vault-pass',
            otp=None, remember_password=False,
        )

    @patch('termius.cli.login_email')
    def test_login_email_otp_prompt(self, login_email):
        login_email.side_effect = [
            ValueError(
                'This account requires otp. Call login again with otp.'
            ),
            {'ok': True, 'username': 'you@example.com'},
        ]
        code = self._run(
            ['login', 'email', '-u', 'you@example.com'],
            prompt=lambda message: '123456',
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 0)
        self.assertEqual(login_email.call_count, 2)
        self.assertEqual(login_email.call_args[1]['otp'], '123456')

    @patch('termius.cli.login_email')
    def test_login_email_uses_env_password(self, login_email):
        os.environ[VAULT_ENV] = 'env-pass'
        login_email.return_value = {'ok': True, 'username': 'you@example.com'}
        code = self._run(['login', 'email', '-u', 'you@example.com'])
        self.assertEqual(code, 0)
        login_email.assert_called_once_with(
            self.runtime, 'you@example.com', 'env-pass',
            otp=None, remember_password=True,
        )

    @patch('termius.cli.login_email')
    def test_login_email_prompts_username(self, login_email):
        login_email.return_value = {'ok': True, 'username': 'you@example.com'}
        code = self._run(
            ['login', 'email'],
            prompt=lambda message: 'you@example.com',
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 0)
        login_email.assert_called_once_with(
            self.runtime, 'you@example.com', 'vault-pass',
            otp=None, remember_password=True,
        )

    def test_login_email_requires_username_without_tty(self):
        code = self._run(['login', 'email'], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn('username is required', self.stderr.getvalue())

    def test_login_requires_method_without_tty(self):
        code = self._run(['login'], isatty=False)
        self.assertEqual(code, 1)
        self.assertIn('specify google or email', self.stderr.getvalue())

    def test_login_username_selects_email(self):
        with patch('termius.cli.login_email') as login_email:
            login_email.return_value = {
                'ok': True, 'username': 'you@example.com',
            }
            code = self._run(
                ['login', '-u', 'you@example.com'],
                secret=lambda message: 'vault-pass',
                isatty=False,
            )
        self.assertEqual(code, 0)
        login_email.assert_called_once()

    @patch('termius.cli.login_email')
    def test_login_email_api_error(self, login_email):
        login_email.side_effect = ApiError('bad credentials')
        code = self._run(
            ['login', 'email', '-u', 'you@example.com'],
            secret=lambda message: 'vault-pass',
        )
        self.assertEqual(code, 1)
        self.assertIn('Login failed: bad credentials', self.stderr.getvalue())

    def test_status_not_signed_in(self):
        code = self._run(['status'])
        self.assertEqual(code, 0)
        self.assertIn('Not signed in', self.stdout.getvalue())

    def test_logout(self):
        self.runtime.config.set('User', 'username', 'you@example.com')
        self.runtime.config.set('User', 'apikey', 'token')
        self.runtime.config.write()
        code = self._run(['logout'])
        self.assertEqual(code, 0)
        self.assertIn('Signed out', self.stdout.getvalue())
        self.assertEqual(
            self.runtime.config.get_safe('User', 'username', default=''),
            '',
        )

    @patch('termius.main.run_stdio')
    def test_main_no_args_starts_mcp(self, stdio):
        self.assertEqual(main([]), 0)
        stdio.assert_called_once()

    def test_main_refuses_to_start_when_secrets_cannot_be_opened(self):
        from termius.keychain import SecretStoreError
        with patch(
            'termius.mcp.server.Runtime',
            side_effect=SecretStoreError('bound to the machine'),
        ):
            with patch('sys.stderr', self.stderr):
                code = main([])
        self.assertEqual(code, 1)
        self.assertIn('bound to the machine', self.stderr.getvalue())
        self.assertIn('termius:', self.stderr.getvalue())

    @patch('termius.cli.login_email')
    def test_main_login_dispatch(self, login_email):
        login_email.return_value = {'ok': True, 'username': 'you@example.com'}
        with patch('termius.cli.Runtime', return_value=self.runtime):
            with patch('sys.stdout', self.stdout):
                with patch('sys.stderr', self.stderr):
                    with patch(
                        'termius.cli.getpass.getpass',
                        return_value='vault-pass',
                    ):
                        code = main(
                            ['login', 'email', '-u', 'you@example.com']
                        )
        self.assertEqual(code, 0)
        login_email.assert_called_once()
