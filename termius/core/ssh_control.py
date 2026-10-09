"""Deadlines and cancellation for SSH and SFTP calls.

A tool call must end within its ``timeout`` and must stop when the MCP
client cancels it. paramiko timeouts apply to one blocking read, so a
command that writes a line every minute never times out on its own. The
helpers here cap the whole call and close the connection from another
thread when the call must stop.
"""
from __future__ import unicode_literals

import threading
import time

from .exceptions import TermiusException


class SshCancelled(TermiusException):
    """The MCP client cancelled the call."""


def close_quiet(resource):
    """Close an SSH or SFTP resource and ignore errors."""
    try:
        resource.close()
    except Exception:  # pylint: disable=broad-except
        pass


class Deadline(object):
    """Wall-clock limit for one tool call."""

    def __init__(self, timeout):
        self.timeout = timeout
        self._end = time.monotonic() + timeout

    def remaining(self):
        """Seconds left. Zero or less means the limit has passed."""
        return self._end - time.monotonic()

    def expired(self):
        return self.remaining() <= 0


class CancelToken(object):
    """Thread-safe cancel flag that closes the resources it holds.

    The MCP reader thread calls ``cancel``. The worker thread registers
    the SSH client, so ``cancel`` unblocks a worker that waits on the
    network.
    """

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._resources = {}
        self._next_key = 0

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            resources = list(self._resources.values())
            self._resources.clear()
        for resource in resources:
            close_quiet(resource)

    def register(self, resource):
        """Close ``resource`` on cancel. Return a key for ``unregister``.

        When the token is already cancelled, close ``resource`` now and
        return None.
        """
        with self._lock:
            if not self._event.is_set():
                key = self._next_key
                self._next_key += 1
                self._resources[key] = resource
                return key
        close_quiet(resource)
        return None

    def unregister(self, key):
        if key is None:
            return
        with self._lock:
            self._resources.pop(key, None)

    def check(self):
        """Raise SshCancelled when the token is cancelled."""
        if self.cancelled:
            raise SshCancelled('Cancelled by the client')


def check_cancel(cancel):
    """Raise SshCancelled when ``cancel`` exists and is cancelled."""
    if cancel is not None:
        cancel.check()


class Watchdog(object):
    """Close ``resource`` when ``seconds`` pass before ``stop``."""

    def __init__(self, resource, seconds):
        self.fired = False
        self._resource = resource
        self._timer = threading.Timer(max(seconds, 0), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        self.fired = True
        close_quiet(self._resource)

    def stop(self):
        self._timer.cancel()
