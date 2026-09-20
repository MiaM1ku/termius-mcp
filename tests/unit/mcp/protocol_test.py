# -*- coding: utf-8 -*-
import io
import json
import tempfile
import unittest

from termius.mcp.protocol import (
    ProtocolError, encode_message, read_message, write_message,
)
from termius.mcp.server import run_stdio
from termius.runtime import Runtime


class ProtocolTest(unittest.TestCase):
    def test_roundtrip(self):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}
        stream = io.BytesIO()
        write_message(stream, payload)
        stream.seek(0)
        self.assertEqual(read_message(stream), payload)

    def test_encode_jsonl(self):
        payload = {'a': 1}
        framed = encode_message(payload)
        self.assertTrue(framed.endswith(b'\n'))
        self.assertNotIn(b'Content-Length', framed)
        self.assertEqual(json.loads(framed.decode('utf-8')), payload)

    def test_accepts_jsonl_line(self):
        raw = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
        self.assertEqual(read_message(io.BytesIO(raw))['method'], 'initialize')

    def test_accepts_lf_headers(self):
        body = b'{"jsonrpc":"2.0","id":1}'
        raw = b'Content-Length: %d\n\n%s' % (len(body), body)
        stream = io.BytesIO(raw)
        self.assertEqual(read_message(stream)['id'], 1)

    def test_eof(self):
        self.assertIsNone(read_message(io.BytesIO(b'')))

    def test_missing_content_length(self):
        stream = io.BytesIO(b'X-Foo: 1\r\n\r\n{}')
        with self.assertRaises(ProtocolError):
            read_message(stream)

    def test_run_stdio_initialize_and_list(self):
        tmpdir = tempfile.TemporaryDirectory()
        try:
            runtime = Runtime(directory_path=tmpdir.name)
            stdin = io.BytesIO()
            stdin.write(encode_message({
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {},
            }))
            stdin.write(encode_message({
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
            }))
            stdin.seek(0)
            stdout = io.BytesIO()
            run_stdio(runtime=runtime, stdin=stdin, stdout=stdout)
            stdout.seek(0)
            first = read_message(stdout)
            second = read_message(stdout)
        finally:
            tmpdir.cleanup()
        self.assertEqual(first['result']['serverInfo']['version'], '3.0.0')
        self.assertEqual(first['result']['serverInfo']['title'], 'Termius Cloud')
        self.assertEqual(first['result']['protocolVersion'], '2025-11-25')
        names = [tool['name'] for tool in second['result']['tools']]
        self.assertIn('hosts', names)
        self.assertIn('login', names)
        hosts = [
            tool for tool in second['result']['tools']
            if tool['name'] == 'hosts'
        ][0]
        self.assertEqual(hosts['title'], 'List Hosts')
        self.assertTrue(hosts['description'])
