"""Exercise real SFTP packets using OpenSSH's local subsystem in a temporary tree."""

import io
import json
import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import remote_files
import server
import paramiko
from configuration import load_settings


@unittest.skipUnless(Path('/usr/lib/openssh/sftp-server').exists(), 'OpenSSH sftp-server required')
class RemoteFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ops-hub-sftp-test-')
        self.root = Path(self.temporary.name)
        self.local = self.root / 'local'
        self.remote = self.root / 'remote'
        self.local.mkdir()
        self.remote.mkdir()
        self.config = self.root / 'config'
        self.config.write_text('Host remote-test\n  HostName example.invalid\nHost broken-test\n')
        self.arguments = self.root / 'arguments.json'
        self.ssh = self.root / 'ssh'
        self.ssh.write_text('#!/usr/bin/python3\nimport os, sys, json\n'
                            + 'open(' + repr(str(self.arguments)) + ',"w").write(json.dumps(sys.argv[1:]))\n'
                            + 'if "broken-test" in sys.argv:\n'
                            + ' sys.stderr.write("Permission denied (publickey).\\n");sys.exit(255)\n'
                            + 'os.chdir(' + repr(str(self.remote)) + ')\n'
                            + 'os.execl("/usr/lib/openssh/sftp-server", "sftp-server")\n')
        self.ssh.chmod(0o700)
        self.patches = [patch.object(server, 'SSH_CONFIG', self.config),
                        patch.object(server, 'SSH_EXECUTABLE', str(self.ssh)),
                        patch.object(server, 'HOME_DIR', self.local)]
        for item in self.patches:
            item.start()
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()
        self.port_patch = patch.object(server, 'PORT', self.client.port)
        self.port_patch.start()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/'), 'X-Ops-Hub-Request': '1'}

    async def asyncTearDown(self):
        await self.client.close()
        self.port_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def get(self, endpoint='/api/files', status=200, **parameters):
        response = await self.client.get(endpoint, params={'host': 'remote-test', **parameters})
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def configure_upload_limit(self, mib):
        config = self.root / 'settings.toml'
        config.write_text(f'[files]\nupload_limit_mib = {mib}\n')
        await self.client.close()
        self.port_patch.stop()
        with patch.object(server, 'CONFIG', load_settings(config)):
            self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()
        self.port_patch = patch.object(server, 'PORT', self.client.port)
        self.port_patch.start()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/'), 'X-Ops-Hub-Request': '1'}

    async def action(self, action, status=200, **payload):
        response = await self.client.post('/api/files/action', params={'host': 'remote-test'},
                                          json={'action': action, **payload}, headers=self.headers)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def upload(self, data, filename='上传.bin', status=200):
        form = FormData(quote_fields=False)
        form.add_field('file', io.BytesIO(data), filename=filename)
        response = await self.client.post('/api/files/upload', params={'host': 'remote-test'},
                                          data=form, headers=self.headers)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def test_listing_home_symlinks_unusual_names_and_openssh_options(self):
        (self.remote / '目录').mkdir()
        (self.remote / '中文 "<&>.txt').write_text('远端')
        (self.remote / '.hidden').touch()
        (self.remote / 'link').symlink_to('目录', target_is_directory=True)
        listing = await self.get()
        self.assertEqual(listing['home'], str(self.remote))
        self.assertEqual(len(listing['entries']), 4)
        self.assertTrue(listing['capabilities']['edit'])
        self.assertTrue(listing['capabilities']['trash'])
        self.assertEqual((await self.get(path='~/link'))['path'], str(self.remote / '目录'))
        self.assertEqual((await self.get(path='~'))['path'], str(self.remote))
        arguments = json.loads(self.arguments.read_text())
        self.assertEqual(arguments[-4:], ['-s', '--', 'remote-test', 'sftp'])
        self.assertIn(str(self.config), arguments)
        for option in ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'PermitLocalCommand=no']:
            self.assertIn(option, arguments)
        self.assertEqual(list(self.local.iterdir()), [])

    async def test_create_rename_move_conflicts_and_subtrees(self):
        await self.action('mkdir', name='folder')
        await self.action('create', name='file.txt', content='original')
        await self.action('create', status=409, name='file.txt', content='lost')
        await self.action('mkdir', status=400, name='../unsafe')
        listing = await self.get()
        entry = next(item for item in listing['entries'] if item['name'] == 'file.txt')
        (self.remote / 'existing.txt').write_text('keep')
        await self.action('rename', status=409, path=entry['path'], name='existing.txt', revision=entry['revision'])
        await self.action('rename', path=entry['path'], name='new.txt', revision=entry['revision'])
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'new.txt')
        await self.action('move', path=entry['path'], destination=str(self.remote / 'folder'), revision=entry['revision'])
        self.assertEqual((self.remote / 'folder/new.txt').read_text(), 'original')
        folder = next(item for item in (await self.get())['entries'] if item['name'] == 'folder')
        await self.action('move', status=400, path=folder['path'], destination=folder['path'], revision=folder['revision'])
        await self.action('rename', status=400, path=str(self.remote), name='new-home', revision='any')
        (self.remote / 'stale.txt').write_text('old')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'stale.txt')
        (self.remote / 'stale.txt').write_text('changed length')
        await self.action('rename', status=409, path=entry['path'], name='lost.txt', revision=entry['revision'])
        self.assertEqual((self.remote / 'existing.txt').read_text(), 'keep')

    async def test_upload_binary_empty_duplicate_download_and_text(self):
        data = b'\x00\xff' * (512 * 1024)
        await self.upload(data)
        self.assertEqual((self.remote / '上传.bin').read_bytes(), data)
        await self.upload(b'overwrite', status=409)
        await self.upload(b'', filename='empty.txt')
        self.assertEqual((self.remote / 'empty.txt').stat().st_size, 0)
        response = await self.client.get('/api/files/download', params={'host': 'remote-test', 'path': str(self.remote / '上传.bin')})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), data)
        (self.remote / 'text.txt').write_bytes('中文\r\n'.encode())
        text = await self.get('/api/files/text', path=str(self.remote / 'text.txt'))
        self.assertEqual(text['content'], '中文\r\n')
        self.assertFalse(text.get('readonly', False))
        await self.get('/api/files/text', status=415, path=str(self.remote / '上传.bin'))
        (self.remote / 'text-link').symlink_to('text.txt')
        await self.get('/api/files/text', status=400, path=str(self.remote / 'text-link'))
        await self.get('/api/files/download', status=400, path=str(self.remote / 'text-link'))
        self.assertEqual(list(self.local.iterdir()), [])
        self.assertFalse(list(self.remote.glob('.ops-hub-upload-*')))

    async def test_unknown_hosts_origin_failures_and_unsupported_actions(self):
        for host in ['-oProxyCommand=bad', 'example.com', 'unknown', 'broken-test']:
            result = await self.get(status=502 if host == 'broken-test' else 400, host=host)
            self.assertIn('error', result)
        for action in ['delete', 'save', 'purge', 'unknown-action']:
            await self.action(action, status=400)
        self.assertEqual((await self.get('/api/files/trash'))['entries'], [])
        for headers in [{}, {'Origin': 'http://evil.example', 'X-Ops-Hub-Request': '1'}]:
            response = await self.client.post('/api/files/action', params={'host': 'remote-test'},
                                              json={'action': 'mkdir', 'name': 'forbidden'}, headers=headers)
            self.assertEqual(response.status, 403)
        self.assertFalse((self.remote / 'forbidden').exists())
        self.assertEqual(list(self.local.iterdir()), [])

    async def test_transfer_failure_keeps_destination_and_cleans_temporary(self):
        with patch.object(remote_files.RemoteFiles, 'rename_new', side_effect=OSError(5, 'simulated interruption')):
            await self.upload(b'partial', filename='failed.txt', status=400)
        self.assertFalse((self.remote / 'failed.txt').exists())
        self.assertFalse(list(self.remote.glob('.ops-hub-upload-*')))
        self.assertEqual((await self.get())['upload_limit'], 0)
        await self.configure_upload_limit(1)
        self.assertEqual((await self.get())['upload_limit'], 1024 * 1024)
        await self.upload(b'a' * (1024 * 1024), filename='boundary.bin')
        self.assertEqual((self.remote / 'boundary.bin').stat().st_size, 1024 * 1024)
        error = await self.upload(b'a' * (1024 * 1024 + 1), filename='large.txt', status=413)
        self.assertIn('1 MiB', error['error'])
        self.assertFalse((self.remote / 'large.txt').exists())
        self.assertFalse(list(self.remote.glob('.ops-hub-upload-*')))
        await self.configure_upload_limit(0)
        self.assertEqual((await self.get())['upload_limit'], 0)
        await self.upload(b'a' * (1024 * 1024 + 1), filename='unlimited.bin')
        self.assertEqual((self.remote / 'unlimited.bin').stat().st_size, 1024 * 1024 + 1)

    async def test_pipelined_write_failure_is_not_reported_as_success(self):
        class FailedWriteClient(paramiko.SFTPClient):
            def _async_request(client, fileobj, command, *args):
                identifier = super()._async_request(fileobj, command, *args)
                if command == paramiko.sftp.CMD_WRITE:
                    client.failed_write = identifier
                return identifier

            def _read_response(client, waitfor=None):
                result = super()._read_response(waitfor)
                if waitfor is not None and waitfor == getattr(client, 'failed_write', None):
                    raise OSError(28, 'remote disk full')
                return result

        with patch.object(remote_files.paramiko, 'SFTPClient', FailedWriteClient):
            result = await self.upload(b'last write fails', filename='unconfirmed.txt', status=507)
        self.assertIn('磁盘空间不足', result['error'])
        self.assertFalse((self.remote / 'unconfirmed.txt').exists())
        self.assertFalse(list(self.remote.glob('.ops-hub-upload-*')))

    async def test_edit_backup_restore_permissions_and_content_conflicts(self):
        path = self.remote / 'settings.txt'
        path.write_bytes('中文\r\nsecond\r\n'.encode())
        path.chmod(0o640)
        original_stat = path.stat()
        text = await self.get('/api/files/text', path=str(path))
        saved = await self.action('save', path=str(path), content='updated\r\n', revision=text['revision'])
        self.assertEqual(path.read_bytes(), b'updated\r\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid), (original_stat.st_uid, original_stat.st_gid))
        self.assertEqual((await self.get('/api/files/text', path=str(path)))['revision'], saved['revision'])
        records = (await self.get('/api/files/trash'))['entries']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['operation'], 'edit')
        await self.action('restore', status=409, id=records[0]['id'])
        restored = self.remote / 'restored.txt'
        await self.action('restore', id=records[0]['id'], destination=str(restored))
        self.assertEqual(restored.read_bytes(), '中文\r\nsecond\r\n'.encode())
        self.assertEqual(restored.stat().st_mode & 0o777, 0o640)
        self.assertEqual(int(restored.stat().st_mtime), int(original_stat.st_mtime))
        self.assertEqual(path.read_bytes(), b'updated\r\n')
        current = await self.get('/api/files/text', path=str(path))
        metadata = path.stat()
        path.write_bytes(b'changed\r\n')  # Same length and same SFTP timestamp.
        os.utime(path, (metadata.st_atime, metadata.st_mtime))
        self.assertEqual(path.stat().st_size, metadata.st_size)
        await self.action('save', status=409, path=str(path), content='must not save', revision=current['revision'])
        self.assertEqual(path.read_bytes(), b'changed\r\n')
        self.assertEqual((await self.get('/api/files/trash'))['entries'], [])
        await self.action('save', status=400, path=str(path), content='no version')
        await self.action('save', status=400, path=str(path), content='bad\x00text', revision=current['revision'])
        with patch.object(remote_files, 'TEXT_LIMIT', 2):
            await self.action('save', status=413, path=str(path), content='long', revision=current['revision'])

    async def test_edit_failure_retains_original_and_backup(self):
        path = self.remote / 'settings.txt'
        path.write_text('original')
        text = await self.get('/api/files/text', path=str(path))
        with patch.object(paramiko.SFTPClient, 'posix_rename', side_effect=OSError('Operation unsupported')):
            await self.action('save', status=400, path=str(path), content='updated', revision=text['revision'])
        self.assertEqual(path.read_text(), 'original')
        records = (await self.get('/api/files/trash'))['entries']
        self.assertEqual(len(records), 1)
        self.assertEqual((self.remote / remote_files.TRASH_NAME / records[0]['id'] / 'data').read_text(), 'original')
        self.assertFalse(list(self.remote.glob('.ops-hub-write-*')))
        archive = remote_files.RemoteFiles.archive
        def modified_after_backup(connection, *args):
            result = archive(connection, *args)
            path.write_text('external modification')
            return result
        with patch.object(remote_files.RemoteFiles, 'archive', modified_after_backup):
            await self.action('save', status=409, path=str(path), content='lost', revision=text['revision'])
        self.assertEqual(path.read_text(), 'external modification')
        self.assertEqual(len((await self.get('/api/files/trash'))['entries']), 2)
        self.assertFalse(list(self.remote.glob('.ops-hub-write-*')))

    async def test_trash_directory_links_restore_and_permanent_delete(self):
        folder = self.remote / 'folder'
        folder.mkdir()
        nested = folder / 'nested'
        nested.mkdir()
        (nested / 'data.txt').write_text('nested content')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        (folder / 'link').symlink_to(outside, target_is_directory=True)
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'folder')
        record = await self.action('delete', path=str(folder), revision=entry['revision'])
        self.assertFalse(folder.exists())
        self.assertTrue((self.remote / remote_files.TRASH_NAME / record['id'] / 'data/nested/data.txt').exists())
        await self.action('restore', id=record['id'])
        self.assertEqual((nested / 'data.txt').read_text(), 'nested content')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'folder')
        record = await self.action('delete', path=str(folder), revision=entry['revision'])
        await self.action('purge', id=record['id'])
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')
        self.assertFalse((self.remote / remote_files.TRASH_NAME / record['id']).exists())
        link = self.remote / 'broken-link'
        link.symlink_to('missing')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'broken-link')
        record = await self.action('delete', path=str(link), revision=entry['revision'])
        self.assertTrue((await self.get('/api/files/trash'))['entries'][0]['recoverable'])
        await self.action('restore', id=record['id'])
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), 'missing')
        self.assertEqual(list(self.local.iterdir()), [])

    async def test_trash_protected_paths_links_and_record_identifiers(self):
        path = self.remote / 'file.txt'
        path.write_text('keep')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'file.txt')
        record = await self.action('delete', path=str(path), revision=entry['revision'])
        trash = self.remote / remote_files.TRASH_NAME
        self.assertEqual(trash.stat().st_mode & 0o777, 0o700)
        container = trash / record['id']
        self.assertEqual(container.stat().st_mode & 0o777, 0o700)
        await self.get(path=str(trash), status=403)
        for source in [Path('/'), self.remote, self.remote / '.local']:
            await self.action('delete', status=400, path=str(source), revision='any')
        for operation in ['restore', 'purge']:
            await self.action(operation, status=400, id='../outside')
        external = self.root / 'external-trash'
        external.mkdir()
        (external / 'keep').write_text('keep')
        linked_id = 'a' * 32
        (trash / linked_id).symlink_to(external, target_is_directory=True)
        await self.action('purge', status=400, id=linked_id)
        await self.action('restore', status=400, id=linked_id)
        (container / 'record.json').write_text(json.dumps({**record, 'id': '../outside'}))
        self.assertEqual((await self.get('/api/files/trash'))['entries'][0]['id'], record['id'])
        await self.action('restore', id=record['id'])
        self.assertEqual(path.read_text(), 'keep')
        self.assertEqual((external / 'keep').read_text(), 'keep')
        (trash / linked_id).unlink()
        shutil.rmtree(self.remote / '.local')
        (self.remote / '.local').symlink_to(external)
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'file.txt')
        await self.action('delete', status=400, path=str(path), revision=entry['revision'])
        await self.get('/api/files/trash', status=400)
        await self.action('empty_trash', status=400)
        self.assertEqual(path.read_text(), 'keep')

    async def test_empty_trash_partial_failure_and_corrupt_records(self):
        records = []
        for name in ['one.txt', 'two.txt']:
            path = self.remote / name
            path.write_text(name)
            entry = next(item for item in (await self.get())['entries'] if item['name'] == name)
            records.append(await self.action('delete', path=str(path), revision=entry['revision']))
        trash = self.remote / remote_files.TRASH_NAME
        failed_id = records[0]['id']
        failed_container = trash / failed_id
        remove_tree = remote_files.RemoteFiles.remove_tree
        def failed_remove(connection, path):
            if path == str(failed_container / 'data'):
                raise PermissionError(13, 'denied')
            return remove_tree(connection, path)
        with patch.object(remote_files.RemoteFiles, 'remove_tree', failed_remove):
            result = await self.action('empty_trash')
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(result['errors'][0]['id'], failed_id)
        self.assertTrue((failed_container / 'record.json').exists())
        self.assertTrue((failed_container / 'data').exists())
        (failed_container / 'record.json').write_text('bad JSON')
        listing = (await self.get('/api/files/trash'))['entries']
        self.assertFalse(listing[0]['recoverable'])
        await self.action('restore', status=400, id=failed_id)
        await self.action('purge', id=failed_id)
        orphan = trash / ('b' * 32)
        orphan.mkdir()
        (orphan / 'record.json').write_text(json.dumps({**records[0], 'time': 10**400}))
        self.assertFalse((await self.get('/api/files/trash'))['entries'][0]['recoverable'])
        result = await self.action('empty_trash')
        self.assertEqual(result, {'deleted': 1, 'errors': []})
        self.assertEqual((await self.get('/api/files/trash'))['entries'], [])

    async def test_cross_filesystem_trash_failure_keeps_source(self):
        path = self.remote / 'source.txt'
        path.write_text('preserved')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'source.txt')
        rename = remote_files.RemoteFiles.rename_new
        def different_filesystem(connection, source, destination):
            if source == str(path):
                raise OSError(18, 'cross device')
            return rename(connection, source, destination)
        with patch.object(remote_files.RemoteFiles, 'rename_new', different_filesystem):
            await self.action('delete', status=400, path=str(path), revision=entry['revision'])
        self.assertEqual(path.read_text(), 'preserved')
        self.assertEqual((await self.get('/api/files/trash'))['entries'], [])

    async def test_concurrent_saves_have_one_winner(self):
        path = self.remote / 'shared.txt'
        path.write_text('original')
        text = await self.get('/api/files/text', path=str(path))
        async def save(content):
            response = await self.client.post('/api/files/action', params={'host': 'remote-test'},
                json={'action': 'save', 'path': str(path), 'content': content, 'revision': text['revision']}, headers=self.headers)
            await response.read()
            return response.status
        statuses = await asyncio.gather(save('first update'), save('second update'))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertIn(path.read_text(), ['first update', 'second update'])
        self.assertEqual(len((await self.get('/api/files/trash'))['entries']), 1)

    async def test_restore_cleanup_warning_and_linked_record_purge(self):
        path = self.remote / 'recover.txt'
        path.write_text('recoverable')
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'recover.txt')
        record = await self.action('delete', path=str(path), revision=entry['revision'])
        with patch.object(remote_files.RemoteFiles, 'purge_record', side_effect=PermissionError(13, 'denied')):
            restored = await self.action('restore', id=record['id'])
        self.assertTrue(restored['warning'])
        self.assertEqual(path.read_text(), 'recoverable')
        listing = (await self.get('/api/files/trash'))['entries']
        self.assertFalse(listing[0]['recoverable'])
        await self.action('purge', id=record['id'])
        entry = next(item for item in (await self.get())['entries'] if item['name'] == 'recover.txt')
        record = await self.action('delete', path=str(path), revision=entry['revision'])
        container = self.remote / remote_files.TRASH_NAME / record['id']
        outside = self.root / 'outside-record.json'
        outside.write_text(json.dumps(record))
        (container / 'record.json').unlink()
        (container / 'record.json').symlink_to(outside)
        self.assertFalse((await self.get('/api/files/trash'))['entries'][0]['recoverable'])
        await self.action('restore', status=400, id=record['id'])
        await self.action('purge', id=record['id'])
        self.assertEqual(json.loads(outside.read_text())['id'], record['id'])

    async def test_remote_close_failure_does_not_publish_upload(self):
        class FailedCloseClient(paramiko.SFTPClient):
            def _async_request(client, fileobj, command, *args):
                identifier = super()._async_request(fileobj, command, *args)
                if command == paramiko.sftp.CMD_CLOSE:
                    client.failed_close = identifier
                return identifier

            def _read_response(client, waitfor=None):
                result = super()._read_response(waitfor)
                if waitfor is not None and waitfor == getattr(client, 'failed_close', None):
                    raise OSError(28, 'remote close failed')
                return result

        with patch.object(remote_files.paramiko, 'SFTPClient', FailedCloseClient):
            await self.upload(b'not confirmed', filename='unconfirmed-close.txt', status=507)
        self.assertFalse((self.remote / 'unconfirmed-close.txt').exists())
        self.assertFalse(list(self.remote.glob('.ops-hub-upload-*')))


if __name__ == '__main__':
    unittest.main()
