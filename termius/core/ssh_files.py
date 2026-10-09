"""Copy and manage files on a Termius host over SFTP."""
from __future__ import unicode_literals

import base64
import os
import stat

from .exceptions import TermiusException
from .ssh_control import (
    Deadline, Watchdog, check_cancel, close_quiet,
)
from .ssh_exec import connect_host

MAX_CONTENT = 200000
MAX_TRANSFER = 50 * 1024 * 1024


class SshFileError(TermiusException):
    """SFTP operation failed after the SSH session existed."""


def _join_remote(base, rel):
    if not rel:
        return base
    if rel.startswith('/'):
        return rel
    if base.endswith('/'):
        return base + rel
    return '{}/{}'.format(base, rel)


def _tilde_path(path):
    if path is None or str(path).strip() == '':
        return '.'
    raw = str(path)
    if raw == '~':
        return '.'
    if raw.startswith('~/'):
        return raw[2:] or '.'
    return raw


def _absolute_remote(sftp, raw):
    if raw.startswith('/'):
        return raw
    try:
        home = sftp.normalize('.')
    except OSError:
        return raw
    return _join_remote(home, raw)


def resolve_remote_path(sftp, path):
    """Expand ``~`` and make ``path`` absolute when the server allows it."""
    raw = _tilde_path(path)
    try:
        return sftp.normalize(raw)
    except OSError:
        return _absolute_remote(sftp, raw)


def _is_dir_mode(mode):
    return stat.S_ISDIR(mode or 0)


def _is_link_mode(mode):
    return stat.S_ISLNK(mode or 0)


def _entry_kind(mode):
    if _is_dir_mode(mode):
        return 'dir'
    if _is_link_mode(mode):
        return 'link'
    return 'file'


def _entry_from_attr(item, name=None):
    mode = item.st_mode or 0
    filename = name or getattr(item, 'filename', None) or ''
    return {
        'name': filename,
        'type': _entry_kind(mode),
        'size': item.st_size,
        'mtime': item.st_mtime,
        'mode': stat.filemode(mode) if mode else None,
    }


def _is_remote_dir(sftp, path):
    try:
        attr = sftp.stat(path)
    except OSError:
        return False
    return _is_dir_mode(attr.st_mode)


def _basename_remote(path):
    trimmed = path.rstrip('/')
    if not trimmed:
        return path
    return trimmed.rsplit('/', 1)[-1]


def _parent_remote(path):
    trimmed = path.rstrip('/')
    if '/' not in trimmed:
        return ''
    return trimmed.rsplit('/', 1)[0]


def _mkdir_p(sftp, path):
    if path in ('', '.', '/'):
        return
    try:
        attr = sftp.stat(path)
    except OSError:
        parent = _parent_remote(path)
        if parent and parent != path:
            _mkdir_p(sftp, parent)
        sftp.mkdir(path)
        return
    if _is_dir_mode(attr.st_mode):
        return
    raise SshFileError('Not a directory: {}'.format(path))


def _rm_tree(sftp, path):
    for item in sftp.listdir_attr(path):
        child = _join_remote(path, item.filename)
        if _is_dir_mode(item.st_mode):
            _rm_tree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def _decode_content(data):
    if b'\x00' in data:
        return base64.b64encode(data).decode('ascii'), 'base64'
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        return base64.b64encode(data).decode('ascii'), 'base64'


def _encode_content(extra):
    content = extra.get('content')
    if content is None:
        raise SshFileError('content is required for write')
    encoding = extra.get('encoding') or 'utf-8'
    if encoding == 'base64':
        return base64.b64decode(content)
    if isinstance(content, bytes):
        return content
    return content.encode('utf-8')


def _require_local_path(extra, action):
    local_path = extra.get('local_path')
    if not local_path or not str(local_path).strip():
        raise SshFileError('local_path is required for {}'.format(action))
    return os.path.abspath(os.path.expanduser(str(local_path)))


def _check_size(size, limit, label):
    if size > limit:
        raise SshFileError(
            '{} is {} bytes; max is {}.'.format(label, size, limit)
        )


def _resolve_local_dest(local_path, basename):
    if os.path.isdir(local_path):
        return os.path.join(local_path, basename)
    return local_path


def _action_list(sftp, path, extra):
    del extra
    entries = [_entry_from_attr(item) for item in sftp.listdir_attr(path)]
    entries.sort(key=lambda row: (row['type'] != 'dir', row['name'].lower()))
    return {'path': path, 'entries': entries, 'count': len(entries)}


def _action_stat(sftp, path, extra):
    del extra
    row = _entry_from_attr(sftp.stat(path), name=_basename_remote(path))
    row['path'] = path
    return row


def _action_read(sftp, path, extra):
    del extra
    size = sftp.stat(path).st_size or 0
    _check_size(size, MAX_CONTENT, 'Remote file')
    with sftp.open(path, 'rb') as handle:
        data = handle.read(MAX_CONTENT + 1)
    _check_size(len(data), MAX_CONTENT, 'Remote file')
    text, encoding = _decode_content(data)
    return {
        'path': path,
        'size': len(data),
        'encoding': encoding,
        'content': text,
    }


def _action_write(sftp, path, extra):
    data = _encode_content(extra)
    _check_size(len(data), MAX_CONTENT, 'Write content')
    parent = _parent_remote(path)
    if extra.get('recursive') and parent and parent != path:
        _mkdir_p(sftp, parent)
    with sftp.open(path, 'wb') as handle:
        handle.write(data)
    return {'path': path, 'size': len(data), 'ok': True}


def _action_get(sftp, path, extra):
    local_path = _require_local_path(extra, 'get')
    if _is_remote_dir(sftp, path):
        raise SshFileError(
            'Remote path is a directory. Pass a file path.'
        )
    size = sftp.stat(path).st_size or 0
    _check_size(size, MAX_TRANSFER, 'Remote file')
    dest = _resolve_local_dest(local_path, _basename_remote(path))
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    sftp.get(path, dest)
    return {
        'path': path,
        'local_path': dest,
        'size': os.path.getsize(dest),
        'ok': True,
    }


def _action_put(sftp, path, extra):
    src = _require_local_path(extra, 'put')
    if not os.path.isfile(src):
        raise SshFileError('Local file not found: {}'.format(src))
    size = os.path.getsize(src)
    _check_size(size, MAX_TRANSFER, 'Local file')
    dest = path
    if _is_remote_dir(sftp, path):
        dest = _join_remote(path, os.path.basename(src))
    parent = _parent_remote(dest)
    if extra.get('recursive') and parent and parent != dest:
        _mkdir_p(sftp, parent)
    sftp.put(src, dest)
    return {
        'path': dest,
        'local_path': src,
        'size': size,
        'ok': True,
    }


def _action_mkdir(sftp, path, extra):
    if extra.get('recursive'):
        _mkdir_p(sftp, path)
    else:
        sftp.mkdir(path)
    return {'path': path, 'ok': True}


def _action_rm(sftp, path, extra):
    attr = sftp.stat(path)
    if _is_dir_mode(attr.st_mode):
        if extra.get('recursive'):
            _rm_tree(sftp, path)
        else:
            sftp.rmdir(path)
    else:
        sftp.remove(path)
    return {'path': path, 'ok': True}


def _action_rename(sftp, path, extra):
    dest = extra.get('dest')
    if not dest or not str(dest).strip():
        raise SshFileError('dest is required for rename')
    dest = resolve_remote_path(sftp, dest)
    sftp.rename(path, dest)
    return {'path': path, 'dest': dest, 'ok': True}


ACTIONS = {
    'list': _action_list,
    'stat': _action_stat,
    'read': _action_read,
    'write': _action_write,
    'get': _action_get,
    'put': _action_put,
    'mkdir': _action_mkdir,
    'rm': _action_rm,
    'rename': _action_rename,
}


def _run_sftp(client, handler, path, extra):
    sftp = client.open_sftp()
    try:
        resolved = resolve_remote_path(sftp, path)
        return handler(sftp, resolved, extra)
    except SshFileError:
        raise
    except Exception as exc:
        raise SshFileError('SFTP failed: {}'.format(exc))
    finally:
        close_quiet(sftp)


def _with_host_meta(payload, target, username, action):
    payload['host'] = target.label
    payload['address'] = target.address
    payload['username'] = username
    payload['action'] = action
    payload.setdefault('ok', True)
    return payload


def run_file_action(target, action, path, timeout=60, extra=None,
                    cancel=None):
    """Run one SFTP action on ``target``.

    ``timeout`` caps the whole call: connect plus transfer. When it
    passes, the connection is closed and SshFileError is raised.
    ``cancel`` is an optional CancelToken; a cancel closes the
    connection and raises SshCancelled.
    """
    handler = ACTIONS.get(action)
    if handler is None:
        raise SshFileError(
            'Unknown files action: {}'.format(action)
        )
    deadline = Deadline(timeout)
    client, username = connect_host(target, timeout=timeout, cancel=cancel)
    key = cancel.register(client) if cancel is not None else None
    watchdog = Watchdog(client, deadline.remaining())
    try:
        payload = _run_sftp(client, handler, path, extra or {})
    except Exception:
        check_cancel(cancel)
        if watchdog.fired:
            raise SshFileError(
                '{} {} on {} timed out after {} s. Pass a larger timeout '
                'for big transfers.'.format(
                    action, path, target.label, timeout,
                )
            )
        raise
    finally:
        watchdog.stop()
        if cancel is not None:
            cancel.unregister(key)
        close_quiet(client)
    return _with_host_meta(payload, target, username, action)
