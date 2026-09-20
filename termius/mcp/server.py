# -*- coding: utf-8 -*-
"""Stdio MCP server for Termius Cloud."""
from __future__ import unicode_literals

import json
import logging
import sys

from .. import __version__
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


def handle_rpc(runtime, message):
    """Return a JSON-RPC response dict, or None for a notification."""
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
            data, summary = call_tool(runtime, name, arguments)
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


def run_stdio(runtime=None, stdin=None, stdout=None):
    """Serve MCP over stdin/stdout until EOF."""
    runtime = runtime or Runtime()
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except ProtocolError as exc:
            LOGGER.warning('Bad MCP frame: %s', exc)
            continue
        if message is None:
            return
        if not isinstance(message, dict):
            continue
        response = handle_rpc(runtime, message)
        if response is not None:
            write_message(stdout, response)
