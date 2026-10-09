# -*- coding: utf-8 -*-
import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from termius.mcp.protocol import encode_message, read_message
from termius.mcp.server import StdioServer
from termius.mcp.tools import ToolError
from termius.runtime import Runtime


class _Sink(object):
    """Thread-safe stdout stand-in that records each written message."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = b''
        self.changed = threading.Condition(self.lock)

    def write(self, data):
        with self.lock:
            self.data += data
            self.changed.notify_all()

    def flush(self):
        pass

    def messages(self):
        with self.lock:
            stream = io.BytesIO(self.data)
        out = []
        while True:
            message = read_message(stream)
            if message is None:
                return out
            out.append(message)

    def wait_for(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = [m for m in self.messages() if predicate(m)]
            if found:
                return found[0]
            time.sleep(0.02)
        return None


def _call(message_id, name, arguments=None):
    return {
        'jsonrpc': '2.0', 'id': message_id, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments or {}},
    }


class StdioServerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runtime = Runtime(directory_path=self.tmpdir.name)
        read_fd, write_fd = os.pipe()
        self.stdin = os.fdopen(read_fd, 'rb')
        self.feed = os.fdopen(write_fd, 'wb')
        self.sink = _Sink()
        self.release = threading.Event()
        self.seen_tokens = {}
        self.server = StdioServer(self.runtime, self.stdin, self.sink)
        self.thread = threading.Thread(target=self.server.serve)
        self.thread.daemon = True

    def tearDown(self):
        self.release.set()
        if not self.feed.closed:
            self.feed.close()
        self.thread.join(5)
        self.stdin.close()
        self.tmpdir.cleanup()

    def _send(self, message):
        self.feed.write(encode_message(message))
        self.feed.flush()

    def _fake_call_tool(self, runtime, name, arguments, cancel=None):
        if name == 'exec':
            self.seen_tokens[arguments.get('tag')] = cancel
            while not self.release.is_set():
                if cancel is not None and cancel.cancelled:
                    raise ToolError('Cancelled by the client', 'cancelled')
                time.sleep(0.01)
            return {'ok': True}, 'exec done'
        return {'tool': name}, '{} done'.format(name)

    def test_slow_exec_does_not_block_other_requests(self):
        with patch('termius.mcp.server.call_tool', self._fake_call_tool):
            self.thread.start()
            self._send(_call(1, 'exec', {'tag': 'slow'}))
            self._send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
            self._send(_call(3, 'status'))
            status = self.sink.wait_for(lambda m: m.get('id') == 3)
            ping = self.sink.wait_for(lambda m: m.get('id') == 2)
            self.assertIsNotNone(status)
            self.assertIsNotNone(ping)
            ids = [m.get('id') for m in self.sink.messages()]
            self.assertNotIn(1, ids)
            self.release.set()
            done = self.sink.wait_for(lambda m: m.get('id') == 1)
        self.assertEqual(done['result']['structuredContent'], {'ok': True})

    def test_cancel_stops_the_call_and_sends_no_response(self):
        with patch('termius.mcp.server.call_tool', self._fake_call_tool):
            self.thread.start()
            self._send(_call('a-1', 'exec', {'tag': 'slow'}))
            deadline = time.monotonic() + 5
            while 'slow' not in self.seen_tokens:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self._send({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 'a-1', 'reason': 'timeout'},
            })
            self._send(_call(9, 'status'))
            self.assertIsNotNone(
                self.sink.wait_for(lambda m: m.get('id') == 9)
            )
            token = self.seen_tokens['slow']
            end = time.monotonic() + 5
            while not token.cancelled and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(token.cancelled)
            self.feed.close()
            self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        ids = [m.get('id') for m in self.sink.messages()]
        self.assertNotIn('a-1', ids)

    def test_eof_waits_for_running_calls(self):
        with patch('termius.mcp.server.call_tool', self._fake_call_tool):
            self.thread.start()
            self._send(_call(5, 'exec', {'tag': 'slow'}))
            self.feed.close()
            time.sleep(0.1)
            self.assertTrue(self.thread.is_alive())
            self.release.set()
            self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertIsNotNone(self.sink.wait_for(lambda m: m.get('id') == 5))

    def test_cancel_for_unknown_request_is_ignored(self):
        self.thread.start()
        self._send({
            'jsonrpc': '2.0', 'method': 'notifications/cancelled',
            'params': {'requestId': 404},
        })
        self._send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertIsNotNone(self.sink.wait_for(lambda m: m.get('id') == 1))
