# -*- coding: utf-8 -*-
"""Stdio MCP server for Termius Cloud."""
from __future__ import unicode_literals

import json
import logging
import sys
import threading

from .. import __version__
from ..core.ssh_control import CancelToken
from ..runtime import Runtime
from .protocol import ProtocolError, read_message, write_message
from .tools import TOOLS, ToolError, call_tool

# OMP and current Cursor speak 2025-11-25. Echo the client's version when we
# implement that revision; otherwise advertise our latest. Always returning
# 2025-06-18 made 2025-11-25-only clients eligible to disconnect after
# initialize (MCP lifecycle: server MUST echo a version it supports).
SUPPORTED_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18')
PROTOCOL_VERSION = '2025-11-25'
LOGGER = logging.getLogger(__name__)

INSTRUCTIONS = (
    'Termius Cloud inventory, SSH exec, and SFTP files. Call status first. '
    'If not signed in, use login (google is two-step: login then '
    'login_complete). hosts, host, exec, files, and inventory auto-pull '
    'the vault when a password is remembered. Never echo vault or host '
    'passwords.'
)


def _json_text(data):
    return json.dumps(data, default=str, separators=(',', ':'))


def _ok(data, summary):
    return {
        'content': [
            {'type': 'text', 'text': summary},
            {'type': 'text', 'text': _json_text(data)},
        ],
        'structuredContent': data,
    }


def _error_result(message, code=None):
    payload = {'error': message}
    if code:
        payload['code'] = code
    return {
        'content': [
            {'type': 'text', 'text': message},
            {'type': 'text', 'text': _json_text(payload)},
        ],
        'structuredContent': payload,
        'isError': True,
    }


def _negotiated_protocol_version(params):
    requested = None
    if isinstance(params, dict):
        requested = params.get('protocolVersion')
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION


def _initialize_result(params=None):
    return {
        'protocolVersion': _negotiated_protocol_version(params),
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {
            'name': 'termius',
            'title': 'Termius Cloud',
            'version': __version__,
        },
        'instructions': INSTRUCTIONS,
    }


def handle_rpc(runtime, message, cancel=None):
    """Return a JSON-RPC response dict, or None for a notification.

    ``cancel`` is an optional CancelToken for a tools/call request.
    """
    method = message.get('method')
    message_id = message.get('id')
    params = message.get('params') or {}
    if method == 'initialize':
        return _rpc_result(message_id, _initialize_result(params))
    if method == 'notifications/initialized':
        return None
    if method == 'tools/list':
        return _rpc_result(message_id, {'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        try:
            data, summary = call_tool(runtime, name, arguments, cancel=cancel)
            return _rpc_result(message_id, _ok(data, summary))
        except ToolError as exc:
            return _rpc_result(message_id, _error_result(str(exc), exc.code))
        except Exception as exc:
            LOGGER.exception('Tool %s failed', name)
            return _rpc_result(
                message_id, _error_result(str(exc), 'internal_error')
            )
    if method == 'ping':
        return _rpc_result(message_id, {})
    if message_id is not None:
        return {
            'jsonrpc': '2.0',
            'id': message_id,
            'error': {
                'code': -32601,
                'message': 'Method not found: {}'.format(method),
            },
        }
    return None


def _rpc_result(message_id, result):
    return {'jsonrpc': '2.0', 'id': message_id, 'result': result}


def _request_key(message_id):
    """Key for an in-flight request. JSON-RPC ids are strings or numbers."""
    return json.dumps(message_id, sort_keys=True)


class StdioServer(object):
    """MCP stdio server that runs each tools/call in its own thread.

    The reader thread answers initialize, tools/list, and ping at once,
    so a slow SSH command does not block other requests. A
    notifications/cancelled message cancels the matching call: its SSH
    connection is closed and no response is sent, as the MCP spec asks.
    """

    def __init__(self, runtime, stdin, stdout):
        self.runtime = runtime
        self.stdin = stdin
        self.stdout = stdout
        self._write_lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._calls = {}
        self._threads = []

    def serve(self):
        """Serve until EOF on stdin, then wait for running calls."""
        while True:
            try:
                message = read_message(self.stdin)
            except ProtocolError as exc:
                LOGGER.warning('Bad MCP frame: %s', exc)
                continue
            if message is None:
                break
            if isinstance(message, dict):
                self.dispatch(message)
        self.wait()

    def dispatch(self, message):
        method = message.get('method')
        if method == 'tools/call' and message.get('id') is not None:
            self._start_call(message)
            return
        if method == 'notifications/cancelled':
            self.cancel(message.get('params') or {})
            return
        response = handle_rpc(self.runtime, message)
        if response is not None:
            self._write(response)

    def cancel(self, params):
        """Cancel the in-flight request named by ``params.requestId``."""
        if not isinstance(params, dict) or 'requestId' not in params:
            return
        key = _request_key(params.get('requestId'))
        with self._calls_lock:
            token = self._calls.get(key)
        if token is None:
            return
        LOGGER.info(
            'Cancelling request %s: %s', key, params.get('reason') or '',
        )
        token.cancel()

    def wait(self, timeout=None):
        """Wait for running calls. Each call ends within its own timeout."""
        with self._calls_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    def _start_call(self, message):
        key = _request_key(message.get('id'))
        token = CancelToken()
        thread = threading.Thread(
            target=self._run_call,
            args=(key, token, message),
            name='mcp-call-{}'.format(key),
        )
        thread.daemon = True
        with self._calls_lock:
            self._calls[key] = token
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run_call(self, key, token, message):
        try:
            response = handle_rpc(self.runtime, message, cancel=token)
        except BaseException as exc:  # pylint: disable=broad-except
            LOGGER.exception('Request %s failed', key)
            response = _rpc_result(
                message.get('id'), _error_result(str(exc), 'internal_error'),
            )
        finally:
            with self._calls_lock:
                if self._calls.get(key) is token:
                    del self._calls[key]
        if token.cancelled:
            return
        self._write(response)

    def _write(self, payload):
        with self._write_lock:
            try:
                write_message(self.stdout, payload)
            except (OSError, ValueError) as exc:
                LOGGER.warning('Could not write MCP response: %s', exc)


def run_stdio(runtime=None, stdin=None, stdout=None):
    """Serve MCP over stdin/stdout until EOF."""
    runtime = runtime or Runtime()
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    StdioServer(runtime, stdin, stdout).serve()
