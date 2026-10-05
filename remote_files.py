"""SFTP file operations over the system OpenSSH client's authenticated stream."""

import asyncio
import errno
import hashlib
import os
import posixpath
import select
import signal
import socket
import stat
import subprocess
import tempfile
import uuid
from urllib.parse import quote

import paramiko
from aiohttp import web

from file_manager import ENTRY_LIMIT, TEXT_LIMIT, UPLOAD_LIMIT, name_value


class SFTPStream:
    """Expose the channel methods Paramiko needs on a subprocess socket."""

    def __init__(self, connection):
        self.connection = connection

    def get_name(self):
        return 'openssh'

    def recv_ready(self):
        return bool(select.select([self.connection], [], [], 0)[0])

    def __getattr__(self, name):
        return getattr(self.connection, name)


def remote_revision(metadata):
    # SFTP v3 exposes timestamps in whole seconds and has no inode/ctime field.
    fields = tuple(getattr(metadata, key, None) for key in
                   ('st_mode', 'st_size', 'st_mtime', 'st_uid', 'st_gid'))
    return hashlib.sha256(repr(fields).encode()).hexdigest()


def inside(path, directory):
    return path == directory or path.startswith(directory.rstrip('/') + '/')


class RemoteFiles:
    def __init__(self, host, config, executable):
        self.host, self.config, self.executable = host, config, executable
        self.socket = self.process = self.errors = self.client = None
        self.home = None

    def connect(self):
        if not self.executable:
            raise web.HTTPServiceUnavailable(text='未找到系统 OpenSSH 客户端。')
        parent, child = socket.socketpair()
        self.socket = parent
        parent.settimeout(30)
        self.errors = tempfile.TemporaryFile()
        arguments = [self.executable, '-F', str(self.config), '-T',
                     '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                     '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
                     '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                     '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
                     '-s', '--', self.host, 'sftp']
        try:
            self.process = subprocess.Popen(arguments, stdin=child, stdout=child,
                                            stderr=self.errors, start_new_session=True)
        finally:
            child.close()
        try:
            self.client = paramiko.SFTPClient(SFTPStream(parent))
            self.home = self.client.normalize('.')
            if not self.home.startswith('/'):
                raise ValueError('远端未提供有效的绝对目录路径。')
        except (OSError, EOFError, paramiko.SSHException) as error:
            self.errors.seek(0)
            detail = self.errors.read(4096).decode('utf-8', 'replace').strip()
            message = 'SSH/SFTP 连接失败。请检查网络、密钥认证及远端 SFTP 服务。'
            if 'Host key verification failed' in detail:
                message = '主机密钥尚未信任或发生变化，请先在 SSH 终端核验。'
            elif 'Permission denied' in detail:
                message = 'SSH 认证失败。文件管理需要可用的密钥或 SSH agent，不支持交互输入密码。'
            raise web.HTTPBadGateway(text=message + ('\n' + detail if detail else '')) from error

    def close(self):
        if self.socket:
            self.socket.close()
        if self.process:
            # Also stop a ProxyJump child if the request or transfer is cancelled.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.errors:
            self.errors.close()

    async def call(self, function, *arguments):
        try:
            return await asyncio.to_thread(function, *arguments)
        except (TimeoutError, EOFError, paramiko.SSHException) as error:
            raise web.HTTPBadGateway(text='远程文件连接中断或超时，请刷新后重试。') from error
        except OSError as error:
            if error.errno in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN, errno.EBADF):
                raise web.HTTPBadGateway(text='远程文件连接中断，请刷新后重试。') from error
            if error.errno is None:
                raise web.HTTPBadRequest(text='远端文件操作失败：' + str(error)) from error
            raise

    def path(self, value='', follow=False):
        if not isinstance(value, str) or '\x00' in value:
            raise ValueError('无效的文件路径。')
        value = value or self.home
        if value == '~' or value.startswith('~/'):
            value = self.home + value[1:]
        if not value.startswith('/'):
            raise ValueError('请输入绝对路径，或使用 ~/ 开头的路径。')
        value = posixpath.normpath(value)
        resolved = self.client.normalize(value if follow else posixpath.dirname(value))
        if not follow:
            resolved = posixpath.join(resolved, posixpath.basename(value))
        trash = posixpath.join(self.home, '.local/share/ops-hub/trash')
        if inside(resolved, trash):
            raise web.HTTPForbidden(text='远程回收站管理尚未接入，请通过终端管理这些记录。')
        return resolved

    def ensure_new(self, path):
        try:
            self.client.lstat(path)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return
            raise
        raise web.HTTPConflict(text='同名文件或目录已存在，请使用其他名称。')

    def rename_new(self, source, destination):
        self.ensure_new(destination)
        # Standard SFTP RENAME refuses existing destinations. Never use posix_rename.
        try:
            self.client.rename(source, destination)
        except OSError:
            self.ensure_new(destination)
            raise

    def listing_data(self, value):
        path = self.path(value, follow=True)
        entries = []
        truncated = False
        for metadata in self.client.listdir_iter(path, read_aheads=1):
            if len(entries) == ENTRY_LIMIT:
                truncated = True
                break
            name = name_value(metadata.filename)
            entry_path = posixpath.join(path, name)
            mode = metadata.st_mode or 0
            kind = ('directory' if stat.S_ISDIR(mode) else 'file' if stat.S_ISREG(mode)
                    else 'symlink' if stat.S_ISLNK(mode) else 'special')
            protected = inside(entry_path, posixpath.join(self.home, '.local/share/ops-hub/trash'))
            target_directory, link_target = False, None
            if kind == 'symlink':
                try:
                    link_target = self.client.readlink(entry_path)
                    target_directory = stat.S_ISDIR(self.client.stat(entry_path).st_mode or 0)
                except OSError:
                    pass
            entries.append({'name': name, 'path': entry_path, 'kind': kind,
                            'target_directory': target_directory, 'link_target': link_target,
                            'size': metadata.st_size, 'modified': metadata.st_mtime,
                            'mode': stat.filemode(mode), 'revision': remote_revision(metadata),
                            'protected': protected})
        entries.sort(key=lambda item: (not (item['kind'] == 'directory' or item['target_directory']), item['name'].casefold()))
        return {'path': path, 'parent': posixpath.dirname(path), 'root': '/', 'home': self.home,
                'entries': entries, 'truncated': truncated, 'text_limit': TEXT_LIMIT,
                'upload_limit': UPLOAD_LIMIT, 'capabilities': {'edit': False, 'trash': False}}

    async def listing(self, request):
        return web.json_response(await self.call(self.listing_data, request.query.get('path', '')))

    def open_regular(self, value):
        path = self.path(value)
        before = self.client.lstat(path)
        if not stat.S_ISREG(before.st_mode or 0):
            raise ValueError('仅支持普通文件，请打开符号链接的目标路径。')
        stream = self.client.open(path, 'rb')
        try:
            metadata = stream.stat()
            if (not stat.S_ISREG(metadata.st_mode or 0)
                    or remote_revision(before) != remote_revision(metadata)):
                raise web.HTTPConflict(text='文件在打开时发生变化，请刷新后重试。')
        except BaseException:
            stream.close()
            raise
        return path, stream, metadata

    def text_data(self, value):
        path, stream, metadata = self.open_regular(value)
        with stream:
            if (metadata.st_size or 0) > TEXT_LIMIT:
                raise web.HTTPRequestEntityTooLarge(max_size=TEXT_LIMIT, actual_size=metadata.st_size,
                                                   text='文本查看限制为 2 MiB，请下载后查看。')
            data = stream.read(TEXT_LIMIT + 1)
            if len(data) > TEXT_LIMIT:
                raise ValueError('文本查看限制为 2 MiB。')
            if remote_revision(metadata) != remote_revision(stream.stat()):
                raise web.HTTPConflict(text='读取时文件发生变化，请重新打开。')
        if b'\x00' in data:
            raise web.HTTPUnsupportedMediaType(text='该文件包含二进制内容，请使用下载功能。')
        return {'path': path, 'content': data.decode('utf-8'), 'revision': remote_revision(metadata), 'readonly': True}

    async def text(self, request):
        return web.json_response(await self.call(self.text_data, request.query.get('path', '')))

    async def download(self, request):
        path, stream, metadata = await self.call(self.open_regular, request.query.get('path', ''))
        response = web.StreamResponse(headers={
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(posixpath.basename(path), safe=''),
        })
        try:
            await response.prepare(request)
            while chunk := await self.call(stream.read, 131072):
                await response.write(chunk)
            await response.write_eof()
        except BaseException:
            # Once streaming begins, a failure must abort the download rather than append JSON.
            if request.transport:
                request.transport.close()
            raise
        finally:
            await self.call(stream.close)
        return response

    def mutate(self, payload):
        action = payload.get('action')
        if action not in ('mkdir', 'create', 'rename', 'move'):
            raise ValueError('远程文件暂不支持编辑、删除或回收站操作。')
        path = self.path(payload.get('path', ''))
        if action in ('mkdir', 'create'):
            directory = self.path(payload.get('path', ''), follow=True)
            destination = self.path(posixpath.join(directory, name_value(payload['name'])))
            self.ensure_new(destination)
            if action == 'mkdir':
                self.client.mkdir(destination, mode=0o755)
            else:
                data = payload.get('content', '')
                if not isinstance(data, str) or '\x00' in data:
                    raise ValueError('内容必须是文本，不能包含空字节。')
                data = data.encode('utf-8')
                if len(data) > TEXT_LIMIT:
                    raise ValueError('文本文件限制为 2 MiB。')
                self.publish(directory, destination, [data])
            return {'path': destination}
        if path in ('/', self.home) or inside(posixpath.join(self.home, '.local/share/ops-hub/trash'), path):
            raise ValueError('不能移动、重命名根目录、用户主目录和回收站所在目录。')
        expected = payload.get('revision')
        if not isinstance(expected, str) or not expected:
            raise ValueError('缺少文件版本，请刷新后重试。')
        metadata = self.client.lstat(path)
        if remote_revision(metadata) != expected:
            raise web.HTTPConflict(text='文件已被其他程序修改，请刷新后再操作。')
        if action == 'rename':
            destination = self.path(posixpath.join(posixpath.dirname(path), name_value(payload['name'])))
        else:
            directory = self.path(payload['destination'], follow=True)
            if not stat.S_ISDIR(self.client.stat(directory).st_mode or 0):
                raise ValueError('移动目标必须是文件夹。')
            if stat.S_ISDIR(metadata.st_mode or 0) and inside(directory, path):
                raise ValueError('不能将文件夹移入自身或其子目录。')
            destination = self.path(posixpath.join(directory, posixpath.basename(path)))
        if destination == path:
            raise ValueError('文件已位于目标位置。')
        self.rename_new(path, destination)
        return {'path': destination}

    async def action(self, request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('无效的文件操作。')
        return web.json_response(await self.call(self.mutate, payload))

    def publish(self, directory, destination, chunks):
        temporary = posixpath.join(directory, '.ops-hub-upload-' + uuid.uuid4().hex)
        created = False
        try:
            with self.client.open(temporary, 'wx') as stream:
                created = True
                stream.set_pipelined(True)
                for chunk in chunks:
                    stream.write(chunk)
                # Paramiko 4 queues WRITE acknowledgements as NoneType; close() alone
                # can skip their errors. Explicitly drain before publishing the file.
                while stream._reqs:
                    command, _ = self.client._read_response(stream._reqs.popleft())
                    if command != paramiko.sftp.CMD_STATUS:
                        raise paramiko.SSHException('Unexpected SFTP write acknowledgement')
                stream.set_pipelined(False)
            self.rename_new(temporary, destination)
            created = False
        finally:
            if created:
                self.client.remove(temporary)

    async def upload(self, request):
        directory = await self.call(self.path, request.query.get('path', ''), True)
        metadata = await self.call(self.client.stat, directory)
        if not stat.S_ISDIR(metadata.st_mode or 0):
            raise ValueError('请选择上传目录。')
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != 'file' or not part.filename:
            raise ValueError('请选择要上传的文件。')
        destination = await self.call(self.path, posixpath.join(directory, name_value(part.filename)))
        await self.call(self.ensure_new, destination)
        # Spool only on the hub, then transfer. No final remote name until all bytes are acknowledged.
        with tempfile.TemporaryFile() as spool:
            total = 0
            while chunk := await part.read_chunk(65536):
                total += len(chunk)
                if total > UPLOAD_LIMIT:
                    raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_LIMIT, actual_size=total,
                                                       text='单个文件上传限制为 256 MiB。')
                await asyncio.to_thread(spool.write, chunk)
            spool.seek(0)
            await self.call(self.publish, directory, destination, iter(lambda: spool.read(65536), b''))
        return web.json_response({'path': destination, 'size': total})

    async def trash(self, request):
        raise web.HTTPBadRequest(text='远程回收站管理尚未接入。')
