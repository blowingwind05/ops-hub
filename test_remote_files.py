"""Exercise real SFTP packets using OpenSSH's local subsystem in a temporary tree."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import remote_files
import server
import paramiko


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

    async def action(self, action, status=200, **payload):
        response = await self.client.post('/api/files/action', params={'host': 'remote-test'},
                                          json={'action': action, **payload}, headers=self.headers)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def upload(self, data, filename='上传.bin', status=200):
        form = FormData(quote_fields=False)
        form.add_field('file', data, filename=filename)
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
        self.assertFalse(listing['capabilities']['edit'])
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

    async def test_upload_binary_empty_duplicate_download_and_readonly_text(self):
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
        self.assertTrue(text['readonly'])
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
        for action in ['delete', 'save', 'purge', 'empty_trash']:
            await self.action(action, status=400)
        await self.get('/api/files/trash', status=400)
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
        with patch.object(remote_files, 'UPLOAD_LIMIT', 3):
            await self.upload(b'too large', filename='large.txt', status=413)
        self.assertFalse((self.remote / 'large.txt').exists())

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


if __name__ == '__main__':
    unittest.main()
