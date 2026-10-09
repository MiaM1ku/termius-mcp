"""Run a command on a Termius host with paramiko."""
from __future__ import unicode_literals

from collections import namedtuple
from io import StringIO
import time

import paramiko

from .exceptions import TermiusException
from .ssh_control import (
    Deadline, SshCancelled, check_cancel, close_quiet,
)

MAX_OUTPUT = 200000
# Keep at most this many bytes per stream. UTF-8 needs up to 4 bytes per
# character, so this still fills MAX_OUTPUT characters.
MAX_CAPTURE_BYTES = MAX_OUTPUT * 4
READ_CHUNK = 32768
POLL_SECONDS = 0.1
# After exit-status arrives, wait this long for EOF. A background child
# that keeps stdout open would otherwise hold the call until the deadline.
EXIT_GRACE_SECONDS = 2.0

__all__ = [
    'MAX_OUTPUT', 'SshCancelled', 'SshExecError', 'SshTarget',
    'close_quiet', 'connect_host', 'run_host_command', 'ssh_target',
]


class SshExecError(TermiusException):
    """SSH execution failed before a command result existed."""


SshTarget = namedtuple(
    'SshTarget', 'label address port username password pkey',
)


def _load_pkey(identity):
    ssh_key = identity.ssh_key if identity else None
    if not ssh_key or not ssh_key.private_key:
        return None
    data = ssh_key.private_key
    passphrase = ssh_key.passphrase or None
    if passphrase == '':
        passphrase = None
    errors = []
    key_classes = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]
    dss = getattr(paramiko, 'DSSKey', None)
    if dss is not None:
        key_classes.append(dss)
    for cls in key_classes:
        try:
            return cls.from_private_key(StringIO(data), password=passphrase)
        except Exception as exc:
            errors.append('{}: {}'.format(cls.__name__, exc))
    raise SshExecError('Could not parse SSH private key ({})'.format(
        '; '.join(errors)
    ))


def ssh_target(host, ssh_config):
    """Resolve the address and credentials of ``host`` into plain values.

    Call this while you hold the runtime lock. The result does not touch
    storage, so the SSH work can run without the lock.
    """
    identity = ssh_config.identity if ssh_config else None
    username = identity.username if identity else None
    password = identity.password if identity else None
    if not username:
        raise SshExecError(
            'Host {} has no username. Team identities are linked on pull; '
            'call sync or wait for auto-sync.'.format(
                host.label or host.address
            )
        )
    pkey = _load_pkey(identity)
    port = int((ssh_config.port if ssh_config else None) or 22)
    return SshTarget(
        label=host.label or host.address,
        address=host.address,
        port=port,
        username=username,
        password=password or None,
        pkey=pkey,
    )


def connect_host(target, timeout=60, cancel=None):
    """Open an SSH client. The caller must close it.

    ``timeout`` applies to each connect stage (TCP, banner, auth).
    Returns ``(client, username)``.
    """
    check_cancel(cancel)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    use_local_keys = not target.password and target.pkey is None
    key = cancel.register(client) if cancel is not None else None
    try:
        client.connect(
            hostname=target.address,
            port=target.port,
            username=target.username,
            password=target.password,
            pkey=target.pkey,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=use_local_keys,
            look_for_keys=use_local_keys,
        )
        check_cancel(cancel)
    except SshCancelled:
        close_quiet(client)
        raise
    except Exception as exc:
        close_quiet(client)
        check_cancel(cancel)
        raise SshExecError('SSH to {} failed: {}'.format(
            target.address, exc,
        ))
    finally:
        if cancel is not None:
            cancel.unregister(key)
    return client, target.username


def _trim_output(out, err):
    truncated = False
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + '\n...[stdout truncated]...'
        truncated = True
    if len(err) > MAX_OUTPUT:
        err = err[:MAX_OUTPUT] + '\n...[stderr truncated]...'
        truncated = True
    return out, err, truncated


class _Capture(object):
    """Bounded byte buffer for one output stream."""

    def __init__(self):
        self._chunks = []
        self._size = 0
        self.dropped = False

    def add(self, data):
        room = MAX_CAPTURE_BYTES - self._size
        if room <= 0:
            self.dropped = self.dropped or bool(data)
            return
        if len(data) > room:
            data = data[:room]
            self.dropped = True
        self._chunks.append(data)
        self._size += len(data)

    def text(self):
        return b''.join(self._chunks).decode('utf-8', errors='replace')


def _drain(channel, out, err):
    while channel.recv_ready():
        data = channel.recv(READ_CHUNK)
        if not data:
            break
        out.add(data)
    while channel.recv_stderr_ready():
        data = channel.recv_stderr(READ_CHUNK)
        if not data:
            break
        err.add(data)


class _ExitWatch(object):
    """Decide when a running command has ended."""

    def __init__(self):
        self.exited_at = None

    def done(self, channel):
        if channel.closed:
            return True
        if not channel.exit_status_ready():
            return False
        if channel.eof_received:
            return True
        # Exit status without EOF: a background child holds stdout open.
        now = time.monotonic()
        if self.exited_at is None:
            self.exited_at = now
            return False
        return now - self.exited_at >= EXIT_GRACE_SECONDS


def _wait_for_output(channel, deadline, cancel, out, err):
    """Read until the command ends, the deadline passes, or a cancel.

    Returns True when the command ended and False on timeout.
    """
    watch = _ExitWatch()
    while True:
        check_cancel(cancel)
        _drain(channel, out, err)
        if watch.done(channel):
            _drain(channel, out, err)
            return True
        if deadline.expired():
            _drain(channel, out, err)
            return False
        wait = min(POLL_SECONDS, max(deadline.remaining(), 0))
        if watch.exited_at is None:
            channel.status_event.wait(wait)
        else:
            # status_event stays set after exit, so it cannot pace the loop.
            time.sleep(wait)


def _open_channel(client, target, command, deadline):
    try:
        channel = client.get_transport().open_session(
            timeout=max(deadline.remaining(), 1),
        )
        channel.exec_command(command)
        # The command gets EOF on stdin, so a read from stdin cannot hang.
        channel.shutdown_write()
    except Exception as exc:
        raise SshExecError('SSH to {} failed: {}'.format(
            target.address, exc,
        ))
    return channel


def _exec_command(client, target, username, command, deadline, cancel):
    channel = _open_channel(client, target, command, deadline)
    out, err = _Capture(), _Capture()
    try:
        finished = _wait_for_output(channel, deadline, cancel, out, err)
        code = channel.exit_status if finished else None
        if code == -1 and not channel.exit_status_ready():
            code = None
    except SshCancelled:
        raise
    except Exception as exc:
        check_cancel(cancel)
        raise SshExecError('SSH to {} failed: {}'.format(
            target.address, exc,
        ))
    finally:
        close_quiet(channel)
    stdout, stderr, truncated = _trim_output(out.text(), err.text())
    return {
        'host': target.label,
        'address': target.address,
        'username': username,
        'command': command,
        'exit_code': code,
        'stdout': stdout,
        'stderr': stderr,
        'truncated': truncated or out.dropped or err.dropped,
        'timed_out': not finished,
        'timeout': deadline.timeout,
    }


def run_host_command(target, command, timeout=60, cancel=None):
    """Execute ``command`` on ``target``.

    ``timeout`` caps the whole call: connect plus run. When it passes,
    the channel is closed and the result has ``timed_out`` true,
    ``exit_code`` None, and the output read so far. ``cancel`` is an
    optional CancelToken; a cancel closes the connection and raises
    SshCancelled.
    """
    if not command or not str(command).strip():
        raise SshExecError('Command is empty')
    deadline = Deadline(timeout)
    client, username = connect_host(target, timeout=timeout, cancel=cancel)
    key = cancel.register(client) if cancel is not None else None
    try:
        return _exec_command(
            client, target, username, command, deadline, cancel,
        )
    finally:
        if cancel is not None:
            cancel.unregister(key)
        close_quiet(client)
