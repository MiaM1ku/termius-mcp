# -*- coding: utf-8 -*-
import io
import os
import posixpath
import stat
import tempfile
import unittest
from unittest.mock import patch

from termius.core.ssh_files import (
    SshFileError, _action_get, _action_list, _action_mkdir, _action_put,
    _action_read, _action_rename, _action_rm, _action_stat, _action_write,
    _decode_content, _encode_content, resolve_remote_path, run_file_action,
)


class MemorySftp(object):
    def __init__(self, home='/home/u', files=None):
        self.home = home
        self.files = {} if files is None else dict(files)
        self.files.setdefault('/', None)
        self.files.setdefault(home, None)

    def _abs(self, path):
        if not path or path == '.':
            return self.home
        if path.startswith('/'):
            return posixpath.normpath(path)
        return posixpath.normpath(posixpath.join(self.home, path))

    def normalize(self, path):
        abs_path = self._abs(path)
        if abs_path not in self.files:
            raise IOError('No such file')
        return abs_path

    def _attr(self, path):
        data = self.files[path]
        name = path.rsplit('/', 1)[-1] or path
        if data is None:
            mode = stat.S_IFDIR | 0o755
            size = 4096
        else:
            mode = stat.S_IFREG | 0o644
            size = len(data)
        attr = type('Attr', (), {})()
        attr.filename = name
        attr.st_mode = mode
        attr.st_size = size
        attr.st_mtime = 0
        return attr

    def stat(self, path):
        abs_path = self._abs(path)
        if abs_path not in self.files:
            raise IOError('No such file')
        return self._attr(abs_path)

    def listdir_attr(self, path):
        abs_path = self._abs(path)
        if abs_path not in self.files:
            raise IOError('No such file')
        if self.files[abs_path] is not None:
            raise IOError('Not a directory')
        prefix = abs_path.rstrip('/') + '/'
        entries = []
        for key in self.files:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if rest and '/' not in rest:
                entries.append(self._attr(key))
        return entries

    def open(self, path, mode='r'):
        abs_path = self._abs(path)
        if 'w' not in mode:
            if abs_path not in self.files or self.files[abs_path] is None:
                raise IOError('No such file')
            return io.BytesIO(self.files[abs_path])
        handle = io.BytesIO()
        orig_close = handle.close

        def close():
            self.files[abs_path] = handle.getvalue()
            orig_close()

        handle.close = close
        return handle

    def mkdir(self, path):
        abs_path = self._abs(path)
        if abs_path in self.files:
            raise IOError('Exists')
        parent = posixpath.dirname(abs_path)
        if parent and parent not in self.files:
            raise IOError('No parent')
        self.files[abs_path] = None

    def rmdir(self, path):
        abs_path = self._abs(path)
        prefix = abs_path.rstrip('/') + '/'
        for key in self.files:
            if key.startswith(prefix):
                raise IOError('Not empty')
        if self.files.get(abs_path) is not None:
            raise IOError('Not a directory')
        del self.files[abs_path]

    def remove(self, path):
        abs_path = self._abs(path)
        if abs_path not in self.files or self.files[abs_path] is None:
            raise IOError('No such file')
        del self.files[abs_path]

    def rename(self, old, new):
        src = self._abs(old)
        dest = self._abs(new)
        if src not in self.files:
            raise IOError('No such file')
        self.files[dest] = self.files.pop(src)

    def get(self, remote, local):
        data = self.files[self._abs(remote)]
        with open(local, 'wb') as handle:
            handle.write(data)

    def put(self, local, remote):
        with open(local, 'rb') as handle:
            self.files[self._abs(remote)] = handle.read()

    def close(self):
        pass


class FakeHost(object):
    def __init__(self, label='web', address='10.0.0.1'):
        self.label = label
        self.address = address


class FakeClient(object):
    def __init__(self, sftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


class SshFilesTest(unittest.TestCase):
    def setUp(self):
        self.sftp = MemorySftp(files={
            '/home/u/notes.txt': b'hello\n',
            '/home/u/bin': None,
            '/home/u/bin/app': b'\x00elf',
        })

    def test_resolve_tilde(self):
        self.assertEqual(resolve_remote_path(self.sftp, '~'), '/home/u')
        self.assertEqual(
            resolve_remote_path(self.sftp, '~/notes.txt'),
            '/home/u/notes.txt',
        )

    def test_resolve_missing_stays_absolute(self):
        self.assertEqual(
            resolve_remote_path(self.sftp, '~/new.txt'),
            '/home/u/new.txt',
        )

    def test_list_sorts_dirs_first(self):
        data = _action_list(self.sftp, '/home/u', {})
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['entries'][0]['name'], 'bin')
        self.assertEqual(data['entries'][0]['type'], 'dir')
        self.assertEqual(data['entries'][1]['name'], 'notes.txt')

    def test_stat_and_read_text(self):
        info = _action_stat(self.sftp, '/home/u/notes.txt', {})
        self.assertEqual(info['type'], 'file')
        self.assertEqual(info['size'], 6)
        data = _action_read(self.sftp, '/home/u/notes.txt', {})
        self.assertEqual(data['encoding'], 'utf-8')
        self.assertEqual(data['content'], 'hello\n')

    def test_read_binary_is_base64(self):
        data = _action_read(self.sftp, '/home/u/bin/app', {})
        self.assertEqual(data['encoding'], 'base64')
        self.assertEqual(data['content'], 'AGVsZg==')

    def test_write_and_mkdir_recursive(self):
        _action_mkdir(self.sftp, '/home/u/a/b', {'recursive': True})
        _action_write(
            self.sftp, '/home/u/a/b/c.txt',
            {'content': 'ok', 'recursive': False},
        )
        self.assertEqual(self.sftp.files['/home/u/a/b/c.txt'], b'ok')

    def test_write_base64(self):
        payload = _encode_content({'content': 'YWI=', 'encoding': 'base64'})
        self.assertEqual(payload, b'ab')
        text, encoding = _decode_content(b'ab')
        self.assertEqual((text, encoding), ('ab', 'utf-8'))

    def test_write_requires_content(self):
        with self.assertRaises(SshFileError):
            _action_write(self.sftp, '/home/u/x', {})

    def test_rename_and_rm_file(self):
        _action_rename(
            self.sftp, '/home/u/notes.txt', {'dest': '/home/u/old.txt'},
        )
        self.assertIn('/home/u/old.txt', self.sftp.files)
        _action_rm(self.sftp, '/home/u/old.txt', {})
        self.assertNotIn('/home/u/old.txt', self.sftp.files)

    def test_rm_tree(self):
        _action_rm(self.sftp, '/home/u/bin', {'recursive': True})
        self.assertNotIn('/home/u/bin', self.sftp.files)
        self.assertNotIn('/home/u/bin/app', self.sftp.files)

    def test_rm_dir_requires_recursive(self):
        with self.assertRaises(IOError):
            _action_rm(self.sftp, '/home/u/bin', {})

    def test_get_and_put(self):
        tmpdir = tempfile.TemporaryDirectory()
        try:
            dest = os.path.join(tmpdir.name, 'out.txt')
            got = _action_get(
                self.sftp, '/home/u/notes.txt', {'local_path': dest},
            )
            self.assertTrue(os.path.isfile(got['local_path']))
            src = os.path.join(tmpdir.name, 'up.txt')
            with open(src, 'w') as handle:
                handle.write('up')
            put = _action_put(
                self.sftp, '/home/u', {'local_path': src},
            )
            self.assertEqual(put['path'], '/home/u/up.txt')
            self.assertEqual(self.sftp.files['/home/u/up.txt'], b'up')
        finally:
            tmpdir.cleanup()

    def test_get_rejects_directory(self):
        with self.assertRaises(SshFileError):
            _action_get(self.sftp, '/home/u', {'local_path': '/tmp/x'})

    def test_put_missing_local(self):
        with self.assertRaises(SshFileError):
            _action_put(
                self.sftp, '/home/u/x', {'local_path': '/no/such/file'},
            )

    def test_rename_requires_dest(self):
        with self.assertRaises(SshFileError):
            _action_rename(self.sftp, '/home/u/notes.txt', {})

    def test_run_file_action_unknown(self):
        with self.assertRaises(SshFileError):
            run_file_action(FakeHost(), 'chmod', '/tmp')

    def test_run_file_action_list(self):
        client = FakeClient(self.sftp)
        with patch(
            'termius.core.ssh_files.connect_host',
            return_value=(client, 'root'),
        ):
            data = run_file_action(
                FakeHost(), 'list', '~', extra={},
            )
        self.assertTrue(data['ok'])
        self.assertEqual(data['username'], 'root')
        self.assertEqual(data['action'], 'list')
        self.assertEqual(data['count'], 2)
