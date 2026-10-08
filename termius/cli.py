# -*- coding: utf-8 -*-
"""Terminal commands for Termius Cloud login."""
from __future__ import unicode_literals

import argparse
import getpass
import os
import sys

from .core.exceptions import (
    ApiError, AuthyTokenIssue, OptionNotSetException, OtpTokenRequired,
)
from .runtime import Runtime
from .session import (
    login_email, login_google_complete, login_google_start, logout,
)
from .sync import status_payload
from .vault import VAULT_ENV


class CliError(Exception):
    """User-facing CLI failure."""


class CliContext(object):
    """IO and runtime for one CLI invocation."""

    def __init__(self, runtime, prompt, secret, stdout, stderr, isatty):
        self.runtime = runtime
        self.prompt = prompt
        self.secret = secret
        self.stdout = stdout
        self.stderr = stderr
        self.isatty = isatty


_LOGIN_ERRORS = (
    ApiError, AuthyTokenIssue, CliError, OptionNotSetException,
    OtpTokenRequired, ValueError,
)


def build_parser():
    """Return the argparse parser for terminal commands."""
    parser = argparse.ArgumentParser(
        prog='termius',
        description=(
            'Termius Cloud MCP server. With no arguments, speak MCP on '
            'stdin/stdout. Use login to sign in from a terminal.'
        ),
    )
    sub = parser.add_subparsers(dest='command', metavar='command')
    login = sub.add_parser(
        'login',
        help='Sign in with Google, email and password, or OTP',
        description=(
            'Sign in to Termius Cloud from a terminal. google prints an SSO '
            'URL. email prompts for the vault password. If 2FA is on, the '
            'command prompts for OTP.'
        ),
    )
    login.add_argument(
        'method',
        nargs='?',
        choices=('google', 'email'),
        help='google SSO or email/password. Prompt if omitted on a TTY.',
    )
    login.add_argument(
        '-u', '--username',
        help='Termius email (email method)',
    )
    login.add_argument(
        '--otp',
        help='Authenticator / Authy code if 2FA is on',
    )
    login.add_argument(
        '--no-remember',
        action='store_true',
        help='Do not store the vault password in the secret store',
    )
    sub.add_parser(
        'logout',
        help='Sign out and clear local inventory',
    )
    sub.add_parser(
        'status',
        help='Show login state without syncing',
    )
    return parser


def run(argv, runtime=None, prompt=None, secret=None, stdout=None,
        stderr=None, isatty=None):
    """Run a terminal command. Return a process exit code."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(stderr)
        return 2
    context = CliContext(
        runtime=runtime or Runtime(),
        prompt=prompt or _stdin_prompt,
        secret=secret or getpass.getpass,
        stdout=stdout,
        stderr=stderr,
        isatty=sys.stdin.isatty() if isatty is None else isatty,
    )
    return _dispatch(args, context)


def _dispatch(args, context):
    handlers = {
        'login': cmd_login,
        'logout': cmd_logout,
        'status': cmd_status,
    }
    try:
        return handlers[args.command](args, context)
    except _LOGIN_ERRORS as exc:
        return _fail(context.stderr, _error_message(exc))


def cmd_login(args, context):
    """Sign in with Google SSO or email and password."""
    method = _login_method(args, context)
    if method == 'google':
        return cmd_login_google(args, context)
    return cmd_login_email(args, context)


def cmd_login_google(args, context):
    """Print the desktop SSO URL, then finish with a pasted callback."""
    started = login_google_start()
    _print_google_instructions(context.stderr, started['url'])
    callback = context.prompt('Paste callback URL: ').strip()
    if not callback:
        raise CliError('callback URL is required')
    data = _google_complete(
        context.runtime,
        callback,
        _vault_password(context),
        args.otp,
        not args.no_remember,
        context,
    )
    _print_signed_in(context.stdout, data)
    return 0


def cmd_login_email(args, context):
    """Sign in with email, vault password, and OTP when required."""
    data = _email_complete(
        context.runtime,
        _email_username(args, context),
        _vault_password(context),
        args.otp,
        not args.no_remember,
        context,
    )
    _print_signed_in(context.stdout, data)
    return 0


def cmd_logout(args, context):
    """Clear the cloud session and local inventory."""
    del args
    logout(context.runtime)
    _write_line(context.stdout, 'Signed out.')
    return 0


def cmd_status(args, context):
    """Print login state. Does not pull."""
    del args
    data = status_payload(context.runtime)
    if not data['logged_in']:
        _write_line(context.stdout, 'Not signed in.')
        return 0
    stale = 'stale' if data['stale'] else 'fresh'
    _write_line(
        context.stdout,
        'Signed in as {}, {} hosts, cache {}.'.format(
            data['username'] or 'unknown', data['hosts'], stale,
        ),
    )
    return 0


def _login_method(args, context):
    if args.method:
        return args.method
    if args.username:
        return 'email'
    return _prompt_method(context)


def _prompt_method(context):
    if not context.isatty:
        raise CliError('specify google or email')
    raw = context.prompt(
        'Sign-in method (google/email) [google]: '
    ).strip().lower()
    if not raw:
        return 'google'
    if raw not in ('google', 'email'):
        raise CliError('method must be google or email')
    return raw


def _email_username(args, context):
    if args.username:
        username = args.username.strip()
        if username:
            return username
    return _prompt_username(context)


def _prompt_username(context):
    if not context.isatty:
        raise CliError('username is required for email login (use -u)')
    username = context.prompt('Email: ').strip()
    if not username:
        raise CliError('username is required for email login')
    return username


def _vault_password(context):
    env = os.environ.get(VAULT_ENV)
    if env:
        return env
    password = context.secret('Vault encryption password: ')
    if not password:
        raise CliError('vault encryption password is required')
    return password


def _google_complete(runtime, callback, password, otp, remember, context):
    try:
        return login_google_complete(
            runtime, callback, password,
            otp=otp, remember_password=remember,
        )
    except ValueError as exc:
        if not _is_otp_required(exc) or otp:
            raise
        return login_google_complete(
            runtime, callback, password,
            otp=_require_otp(context), remember_password=remember,
        )


def _email_complete(runtime, username, password, otp, remember, context):
    try:
        return login_email(
            runtime, username, password,
            otp=otp, remember_password=remember,
        )
    except ValueError as exc:
        if not _is_otp_required(exc) or otp:
            raise
        return login_email(
            runtime, username, password,
            otp=_require_otp(context), remember_password=remember,
        )


def _is_otp_required(exc):
    return 'requires otp' in str(exc).lower()


def _require_otp(context):
    code = context.prompt('OTP: ').strip()
    if not code:
        raise CliError('otp is required')
    return code


def _print_google_instructions(stream, url):
    _write_line(stream, 'Open this URL in a browser:')
    _write_line(stream, '')
    _write_line(stream, '  {}'.format(url))
    _write_line(stream, '')
    _write_line(stream, 'Sign in with Google.')
    _write_line(
        stream,
        'When the page tries to open Termius, copy the '
        'termius://app/continue-sso?... URL.',
    )
    _write_line(stream, 'Paste that URL below.')


def _print_signed_in(stream, data):
    _write_line(stream, 'Signed in as {}.'.format(data['username']))


def _error_message(exc):
    if isinstance(exc, CliError):
        return str(exc)
    return 'Login failed: {}'.format(exc)


def _fail(stream, message):
    _write_line(stream, message)
    return 1


def _write_line(stream, text):
    stream.write(text)
    if not text.endswith('\n'):
        stream.write('\n')
    stream.flush()


def _stdin_prompt(message):
    # readline reads the tty in raw mode. Without it, canonical mode drops
    # input past MAX_CANON (1024 bytes on macOS), Enter included, and an
    # SSO callback URL is longer than that.
    try:
        import readline  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        pass
    return input(message)
