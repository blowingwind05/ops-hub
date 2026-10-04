"""File API integration tests; all writes use isolated temporary directories."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import file_manager
import server


class FileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir()
        self.work = self.home / 'work'
        self.work.mkdir()
        self.home_patch = patch.object(server, 'HOME_DIR', self.home)
        self.home_patch.start()
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()
        self.port_patch = patch.object(server, 'PORT', self.client.port)
        self.port_patch.start()
        self.headers = {'Origin': str(self.client.make_url('/')).rstrip('/'), 'X-Ops-Hub-Request': '1'}

    async def asyncTearDown(self):
        await self.client.close()
        self.port_patch.stop()
        self.home_patch.stop()
        self.temporary.cleanup()

    async def listing(self, path=None):
        response = await self.client.get('/api/files', params={'path': str(path or self.work)})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def action(self, action, status=200, **payload):
        response = await self.client.post('/api/files/action', json={'action': action, **payload}, headers=self.headers)
        self.assertEqual(response.status, status, await response.text())
        return await response.json()

    async def test_browse_names_hidden_files_symlinks_and_arbitrary_paths(self):
        (self.work / 'folder').mkdir()
        (self.work / '中文 "<&>.txt').write_text('content')
        (self.work / '.hidden').write_text('hidden')
        (self.work / 'link').symlink_to('folder', target_is_directory=True)
        listing = await self.listing()
        self.assertEqual(listing['entries'][0]['kind'], 'directory')
        self.assertEqual(len(listing['entries']), 4)
        link = next(entry for entry in listing['entries'] if entry['name'] == 'link')
        self.assertTrue(link['target_directory'])
        response = await self.client.get('/api/files', params={'path': str(self.work / 'link')})
        self.assertEqual((await response.json())['path'], str(self.work / 'folder'))
        outside = Path(self.temporary.name) / 'outside-home'
        outside.mkdir()
        self.assertEqual((await self.listing(outside))['path'], str(outside))
        await self.action('create', path=str(outside), name='authorized.txt', content='outside')
        self.assertEqual((outside / 'authorized.txt').read_text(), 'outside')

    async def test_create_rename_and_existing_destinations_preserved(self):
        await self.action('mkdir', path=str(self.work), name='目录')
        await self.action('create', path=str(self.work), name='file.txt', content='original')
        await self.action('create', status=409, path=str(self.work), name='file.txt', content='replace')
        (self.work / 'existing.txt').write_text('keep')
        entry = next(entry for entry in (await self.listing())['entries'] if entry['name'] == 'file.txt')
        await self.action('rename', status=409, path=entry['path'], name='existing.txt', revision=entry['revision'])
        self.assertEqual((self.work / 'existing.txt').read_text(), 'keep')
        await self.action('rename', path=entry['path'], name='renamed.txt', revision=entry['revision'])
        self.assertEqual((self.work / 'renamed.txt').read_text(), 'original')
        await self.action('mkdir', status=400, path=str(self.work), name='../escape')

    async def test_edit_backup_restore_conflicts_and_preserves_permissions(self):
        path = self.work / 'settings.txt'
        path.write_bytes(b'first\r\nsecond\r\n')
        path.chmod(0o640)
        os.setxattr(path, 'user.ops-hub-test', b'preserved')
        response = await self.client.get('/api/files/text', params={'path': str(path)})
        text = await response.json()
        self.assertEqual(text['content'], 'first\r\nsecond\r\n')
        await self.action('save', path=str(path), content='updated\n', revision=text['revision'])
        self.assertEqual(path.read_text(), 'updated\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.getxattr(path, 'user.ops-hub-test'), b'preserved')
        response = await self.client.get('/api/files/trash')
        backup = (await response.json())['entries'][0]
        self.assertEqual(backup['operation'], 'edit')
        await self.action('restore', status=409, id=backup['id'])
        restored = self.work / 'restored.txt'
        await self.action('restore', id=backup['id'], destination=str(restored))
        self.assertEqual(restored.read_bytes(), b'first\r\nsecond\r\n')
        await self.action('save', status=409, path=str(path), content='lost', revision=text['revision'])
        self.assertEqual(path.read_text(), 'updated\n')

    async def test_trash_nonempty_directory_symlink_and_restore(self):
        folder = self.work / 'folder'
        folder.mkdir()
        (folder / 'keep.txt').write_text('recoverable')
        entry = (await self.listing())['entries'][0]
        record = await self.action('delete', path=str(folder), revision=entry['revision'])
        self.assertFalse(folder.exists())
        await self.action('restore', id=record['id'])
        self.assertEqual((folder / 'keep.txt').read_text(), 'recoverable')
        link = self.work / 'link'
        link.symlink_to(folder)
        entry = next(entry for entry in (await self.listing())['entries'] if entry['name'] == 'link')
        record = await self.action('delete', path=str(link), revision=entry['revision'])
        self.assertTrue(folder.exists())
        await self.action('restore', id=record['id'])
        self.assertTrue(link.is_symlink())

    async def test_upload_download_duplicates_and_size_cleanup(self):
        async def upload(content, name='上传.bin'):
            form = FormData(quote_fields=False)
            form.add_field('file', content, filename=name, content_type='application/octet-stream')
            return await self.client.post('/api/files/upload', params={'path': str(self.work)}, data=form, headers=self.headers)
        response = await upload(b'\x00\xffbinary')
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((self.work / '上传.bin').read_bytes(), b'\x00\xffbinary')
        response = await upload(b'overwrite')
        self.assertEqual(response.status, 409)
        response = await self.client.get('/api/files/download', params={'path': str(self.work / '上传.bin')})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'\x00\xffbinary')
        self.assertEqual(response.content_type, 'application/octet-stream')
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        with patch.object(file_manager, 'UPLOAD_LIMIT', 4):
            response = await upload(b'too large', 'limit.txt')
            self.assertEqual(response.status, 413)
        self.assertFalse((self.work / 'limit.txt').exists())
        self.assertFalse(list(self.work.glob('.ops-hub-upload-*')))

    async def test_binary_large_special_files_and_links_cannot_be_edited(self):
        (self.work / 'binary').write_bytes(b'\x00\xff')
        (self.work / 'invalid-utf8').write_bytes(b'\xff')
        (self.work / 'large').write_bytes(b'a' * (file_manager.TEXT_LIMIT + 1))
        (self.work / 'link').symlink_to(self.work / 'binary')
        os.mkfifo(self.work / 'fifo')
        for name, expected in [('binary', 415), ('invalid-utf8', 415), ('large', 413), ('link', 400), ('fifo', 400)]:
            response = await self.client.get('/api/files/text', params={'path': str(self.work / name)})
            self.assertEqual(response.status, expected, name)

    async def test_streaming_upload_can_exceed_json_body_limit(self):
        source = Path(self.temporary.name) / 'large-upload.bin'
        size = 17 * 1024 * 1024
        with source.open('wb') as stream:
            stream.truncate(size)
        with source.open('rb') as stream:
            form = FormData()
            form.add_field('file', stream, filename='large-upload.bin', content_type='application/octet-stream')
            response = await self.client.post('/api/files/upload', params={'path': str(self.work)}, data=form, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['size'], size)
        self.assertEqual((self.work / 'large-upload.bin').stat().st_size, size)

    async def test_origin_headers_remote_targets_and_trash_protection(self):
        for headers in [{}, {'Origin': self.headers['Origin']}, {'Origin': 'https://evil.example', 'X-Ops-Hub-Request': '1'}]:
            response = await self.client.post('/api/files/action', json={'action': 'mkdir', 'path': str(self.work), 'name': 'forbidden'}, headers=headers)
            self.assertEqual(response.status, 403)
        self.assertFalse((self.work / 'forbidden').exists())
        response = await self.client.get('/api/files', params={'host': 'prod'})
        self.assertEqual(response.status, 400)
        await self.action('delete', status=400, path=str(self.home), revision=file_manager.revision(self.home.lstat()))
        await self.action('restore', status=400, id='../escape')
        response = await self.client.get('/api/files', params={'path': str(self.home / file_manager.TRASH_NAME)})
        self.assertEqual(response.status, 403)
        (self.work / 'trash-link').symlink_to(self.home / file_manager.TRASH_NAME)
        response = await self.client.get('/api/files', params={'path': str(self.work / 'trash-link')})
        self.assertEqual(response.status, 403)


if __name__ == '__main__':
    unittest.main()
