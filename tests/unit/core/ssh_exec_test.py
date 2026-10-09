# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest.mock import patch

from termius.core.ssh_control import CancelToken, SshCancelled
from termius.core.ssh_exec import SshTarget, run_host_command
from termius.core.ssh_files import SshFileError, run_file_action


TARGET = SshTarget(
    label='web', address='10.0.0.1', port=22, username='root',
    password='pw', pkey=None,
)


class FakeChannel(object):
    """paramiko Channel stand-in driven by a script of timed events."""

    def __init__(self, chunk_every=None, exit_after=None, eof_after=None,
                 exit_code=0):
        self.started = time.monotonic()
        self.chunk_every = chunk_every
        self.exit_after = exit_after
        self.eof_after = eof_after
        self.code = exit_code
        self.status_event = threading.Event()
        self.closed = False
        self.sent = 0
        self.stdin_closed = False
        self.command = None

    def _elapsed(self):
        return time.monotonic() - self.started

    def exec_command(self, command):
        self.command = command

    def shutdown_write(self):
        self.stdin_closed = True

    def _due_chunks(self):
        if self.chunk_every is None:
            return 0
        limit = self._elapsed()
        if self.exit_after is not None:
            limit = min(limit, self.exit_after)
        return int(limit / self.chunk_every) - self.sent

    def recv_ready(self):
        return self._due_chunks() > 0

    def recv(self, nbytes):
        self.sent += 1
        return b'line\n'

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, nbytes):
        return b''

    @property
    def eof_received(self):
        return self.eof_after is not None and self._elapsed() >= self.eof_after

    def exit_status_ready(self):
        ready = (
            self.closed
            or (self.exit_after is not None
                and self._elapsed() >= self.exit_after)
        )
        if ready:
            self.status_event.set()
        return ready

    @property
    def exit_status(self):
        return self.code if self.exit_status_ready() else -1

    def close(self):
        self.closed = True
        self.status_event.set()


class FakeTransport(object):
    def __init__(self, channel):
        self.channel = channel

    def open_session(self, timeout=None):
        return self.channel


class FakeClient(object):
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def get_transport(self):
        return FakeTransport(self.channel)

    def close(self):
        self.closed = True
        self.channel.close()


def _patched_connect(client):
    return patch(
        'termius.core.ssh_exec.connect_host',
        return_value=(client, 'root'),
    )


class RunHostCommandTest(unittest.TestCase):
    def test_normal_exit(self):
        channel = FakeChannel(chunk_every=0.01, exit_after=0.05,
                              eof_after=0.05, exit_code=3)
        client = FakeClient(channel)
        with _patched_connect(client):
            result = run_host_command(TARGET, 'false', timeout=5)
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(result['timed_out'])
        self.assertIn('line', result['stdout'])
        self.assertTrue(channel.stdin_closed)
        self.assertTrue(client.closed)

    def test_trickling_output_still_times_out(self):
        # The command that hung for 40 minutes printed a line now and then,
        # so a per-read timeout never fired. The total deadline must.
        channel = FakeChannel(chunk_every=0.05)
        client = FakeClient(channel)
        started = time.monotonic()
        with _patched_connect(client):
            result = run_host_command(TARGET, 'apk add x', timeout=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['exit_code'])
        self.assertIn('line', result['stdout'])
        self.assertTrue(channel.closed)
        self.assertTrue(client.closed)

    def test_silent_command_times_out(self):
        channel = FakeChannel()
        with _patched_connect(FakeClient(channel)):
            result = run_host_command(TARGET, 'sleep 999', timeout=1)
        self.assertTrue(result['timed_out'])

    def test_exit_without_eof_returns_after_grace(self):
        # A background child keeps stdout open after the shell exits.
        channel = FakeChannel(exit_after=0.05, exit_code=0)
        started = time.monotonic()
        with _patched_connect(FakeClient(channel)):
            with patch('termius.core.ssh_exec.EXIT_GRACE_SECONDS', 0.2):
                result = run_host_command(TARGET, 'daemon &', timeout=10)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['exit_code'], 0)

    def test_cancel_closes_the_connection(self):
        channel = FakeChannel(chunk_every=0.05)
        client = FakeClient(channel)
        token = CancelToken()
        timer = threading.Timer(0.2, token.cancel)
        timer.start()
        started = time.monotonic()
        try:
            with _patched_connect(client):
                with self.assertRaises(SshCancelled):
                    run_host_command(
                        TARGET, 'apk add x', timeout=30, cancel=token,
                    )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(client.closed)

    def test_cancel_before_start(self):
        token = CancelToken()
        token.cancel()
        with self.assertRaises(SshCancelled):
            run_host_command(TARGET, 'true', timeout=5, cancel=token)


class BlockingSftp(object):
    """SFTP stand-in whose listdir blocks until the client is closed."""

    def __init__(self):
        self.unblock = threading.Event()

    def normalize(self, path):
        return '/home/root'

    def listdir_attr(self, path):
        self.unblock.wait(10)
        raise EOFError('connection closed')

    def close(self):
        pass


class BlockingSftpClient(object):
    def __init__(self):
        self.sftp = BlockingSftp()
        self.closed = False

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True
        self.sftp.unblock.set()


class RunFileActionDeadlineTest(unittest.TestCase):
    def _patched(self, client):
        return patch(
            'termius.core.ssh_files.connect_host',
            return_value=(client, 'root'),
        )

    def test_hung_sftp_times_out(self):
        client = BlockingSftpClient()
        started = time.monotonic()
        with self._patched(client):
            with self.assertRaises(SshFileError) as caught:
                run_file_action(TARGET, 'list', '.', timeout=1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn('timed out after 1 s', str(caught.exception))
        self.assertTrue(client.closed)

    def test_cancel_stops_sftp(self):
        client = BlockingSftpClient()
        token = CancelToken()
        timer = threading.Timer(0.2, token.cancel)
        timer.start()
        try:
            with self._patched(client):
                with self.assertRaises(SshCancelled):
                    run_file_action(
                        TARGET, 'list', '.', timeout=30, cancel=token,
                    )
        finally:
            timer.cancel()
        self.assertTrue(client.closed)
