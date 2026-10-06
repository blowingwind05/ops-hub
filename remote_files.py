"""SFTP file operations over the system OpenSSH client's authenticated stream."""

import asyncio
import errno
import hashlib
import json
import os
import posixpath
import re
import select
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import quote

import paramiko
from aiohttp import web

from file_manager import ENTRY_LIMIT, TEXT_LIMIT, TRASH_NAME, UPLOAD_LIMIT, name_value

REMOTE_LOCKS = {}
RECORD_LIMIT = 65536


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


def text_revision(metadata, data):
    return hashlib.sha256(remote_revision(metadata).encode() + b'\x00' + data).hexdigest()


def inside(path, directory):
    return path == directory or path.startswith(directory.rstrip('/') + '/')


class RemoteFiles:
    def __init__(self, host, config, executable):
        self.host, self.config, self.executable = host, config, executable
        self.socket = self.process = self.errors = self.client = None
        self.home = None
        self.write_lock = REMOTE_LOCKS.setdefault((str(config), host), threading.RLock())

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
        trash = posixpath.join(self.home, TRASH_NAME)
        if inside(resolved, trash):
            raise web.HTTPForbidden(text='请通过回收站入口管理这些记录。')
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
            protected = inside(entry_path, posixpath.join(self.home, TRASH_NAME))
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
                'upload_limit': UPLOAD_LIMIT, 'capabilities': {'edit': True, 'trash': True}}

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

    def read_text(self, value):
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
        data.decode('utf-8')
        return path, data, metadata

    def text_data(self, value):
        path, data, metadata = self.read_text(value)
        return {'path': path, 'content': data.decode('utf-8'), 'revision': text_revision(metadata, data)}

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
        with self.write_lock:
            return self.mutate_locked(payload)

    def mutate_locked(self, payload):
        action = payload.get('action')
        if action == 'empty_trash':
            return self.empty_trash()
        if action in ('restore', 'purge'):
            identifier = payload.get('id')
            container = self.trash_container(identifier)
            if action == 'purge':
                self.purge_record(identifier)
                return {'id': identifier}
            record = self.read_record(container, identifier)
            target = self.path(payload.get('destination') or record['path'])
            self.rename_new(posixpath.join(container, 'data'), target)
            warning = None
            try:
                self.purge_record(identifier)
            except (OSError, ValueError, web.HTTPException):
                warning = '文件已恢复，但回收站记录清理失败，可在回收站重试清理。'
            return {'path': target, 'warning': warning}
        if action not in ('mkdir', 'create', 'save', 'rename', 'move', 'delete'):
            raise ValueError('不支持此文件操作。')
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
        if action == 'save':
            return self.save_text(path, payload.get('content'), payload.get('revision'))
        if path in ('/', self.home) or inside(posixpath.join(self.home, TRASH_NAME), path):
            raise ValueError('不能移动、重命名或删除根目录、用户主目录和回收站所在目录。')
        expected = payload.get('revision')
        if not isinstance(expected, str) or not expected:
            raise ValueError('缺少文件版本，请刷新后重试。')
        metadata = self.client.lstat(path)
        if remote_revision(metadata) != expected:
            raise web.HTTPConflict(text='文件已被其他程序修改，请刷新后再操作。')
        if action == 'delete':
            return self.archive(path, 'delete')
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

    def temporary_file(self, directory, chunks, metadata=None, prefix='.ops-hub-upload-'):
        temporary = posixpath.join(directory, prefix + uuid.uuid4().hex)
        created = False
        try:
            with self.client.open(temporary, 'wx') as stream:
                created = True
                stream.set_pipelined(True)
                total = 0
                for chunk in chunks:
                    stream.write(chunk)
                    total += len(chunk)
                # Paramiko 4 queues WRITE acknowledgements as NoneType; close() alone
                # can skip their errors. Explicitly drain before publishing the file.
                while stream._reqs:
                    command, _ = self.client._read_response(stream._reqs.popleft())
                    if command != paramiko.sftp.CMD_STATUS:
                        raise paramiko.SSHException('Unexpected SFTP write acknowledgement')
                stream.set_pipelined(False)
                current = stream.stat()
                if current.st_size != total:
                    raise web.HTTPBadGateway(text='远端写入的文件大小不符，文件未发布。')
                if metadata is not None and (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                    self.client.chown(temporary, metadata.st_uid, metadata.st_gid)
                self.client.chmod(temporary, stat.S_IMODE(metadata.st_mode) if metadata else 0o600)
                # SFTPFile.close() suppresses CLOSE errors too. Require confirmation
                # before the temporary file can become an uploaded file or backup.
                try:
                    self.client._request(paramiko.sftp.CMD_CLOSE, stream.handle)
                finally:
                    stream._closed = True
            created = False
            return temporary
        finally:
            if created:
                self.client.remove(temporary)

    def publish(self, directory, destination, chunks, metadata=None):
        with self.write_lock:
            temporary = self.temporary_file(directory, chunks, metadata)
            try:
                self.rename_new(temporary, destination)
            finally:
                self.remove_if_present(temporary)

    def remove_if_present(self, path):
        try:
            self.client.remove(path)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise

    def save_text(self, path, text, expected):
        if not isinstance(text, str) or '\x00' in text:
            raise ValueError('内容必须是文本，不能包含空字节。')
        data = text.encode('utf-8')
        if len(data) > TEXT_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=TEXT_LIMIT, actual_size=len(data), text='文本编辑限制为 2 MiB。')
        if not isinstance(expected, str) or not expected:
            raise ValueError('缺少文件版本，请重新打开后重试。')
        _, original, metadata = self.read_text(path)
        if text_revision(metadata, original) != expected:
            raise web.HTTPConflict(text='文件已被其他程序修改，请重新打开后再保存。')
        temporary = self.temporary_file(posixpath.dirname(path), [data], metadata, '.ops-hub-write-')
        try:
            self.archive(path, 'edit', original, metadata)
            _, current, current_metadata = self.read_text(path)
            if text_revision(current_metadata, current) != expected:
                raise web.HTTPConflict(text='文件已被其他程序修改，保存已取消；旧版本备份已保留。')
            # OpenSSH extension atomically replaces a file. No unlink/rename fallback.
            try:
                self.client.posix_rename(temporary, path)
            except OSError as error:
                if error.errno is None:
                    raise web.HTTPBadRequest(text='远端拒绝原子替换文件，请检查 SFTP 扩展支持和权限；备份已保留。') from error
                raise
            return {'path': path, 'revision': text_revision(self.client.lstat(path), data)}
        finally:
            self.remove_if_present(temporary)

    def trash_directory(self, create=False):
        directory = self.home
        for component in TRASH_NAME.split('/'):
            directory = posixpath.join(directory, component)
            try:
                metadata = self.client.lstat(directory)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
                if not create:
                    return None
                self.client.mkdir(directory, mode=0o700)
                metadata = self.client.lstat(directory)
            if not stat.S_ISDIR(metadata.st_mode or 0) or self.client.normalize(directory) != directory:
                raise ValueError('回收站路径包含符号链接或非目录，无法执行此操作。')
        return directory

    def trash_container(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('无效的回收站记录。')
        trash = self.trash_directory()
        if trash is None:
            raise FileNotFoundError(errno.ENOENT, 'Trash record missing')
        container = posixpath.join(trash, identifier)
        metadata = self.client.lstat(container)
        if not stat.S_ISDIR(metadata.st_mode or 0) or self.client.normalize(container) != container:
            raise ValueError('回收站记录路径包含符号链接或非目录。')
        return container

    def read_record(self, container, identifier):
        path = posixpath.join(container, 'record.json')
        metadata = self.client.lstat(path)
        if not stat.S_ISREG(metadata.st_mode or 0) or (metadata.st_size or 0) > RECORD_LIMIT:
            raise ValueError('无效的回收站记录。')
        with self.client.open(path, 'rb') as stream:
            data = stream.read(RECORD_LIMIT + 1)
        if len(data) > RECORD_LIMIT:
            raise ValueError('无效的回收站记录。')
        record = json.loads(data)
        if (not isinstance(record, dict) or not isinstance(record.get('path'), str)
                or not record['path'].startswith('/') or '\x00' in record['path']
                or record.get('operation') not in ('edit', 'delete')
                or not isinstance(record.get('time'), (int, float))
                or isinstance(record['time'], bool) or not 0 <= record['time'] <= 253402300799):
            raise ValueError('无效的回收站记录。')
        # The directory name is authoritative; never trust an id supplied by JSON.
        record.update(id=identifier, name=posixpath.basename(record['path']))
        return record

    def archive(self, path, operation, content=None, metadata=None):
        trash = self.trash_directory(create=True)
        identifier = uuid.uuid4().hex
        container = posixpath.join(trash, identifier)
        self.client.mkdir(container, mode=0o700)
        record = {'id': identifier, 'path': path, 'name': posixpath.basename(path),
                  'time': time.time(), 'operation': operation}
        try:
            self.publish(container, posixpath.join(container, 'record.json'),
                         [json.dumps(record, ensure_ascii=True).encode()])
            destination = posixpath.join(container, 'data')
            if operation == 'edit':
                self.publish(container, destination, [content], metadata)
                self.client.utime(destination, (metadata.st_atime, metadata.st_mtime))
            else:
                self.rename_new(path, destination)
        except BaseException:
            try:
                self.client.lstat(posixpath.join(container, 'data'))
            except OSError as error:
                if error.errno == errno.ENOENT:
                    try:
                        self.remove_if_present(posixpath.join(container, 'record.json'))
                        self.client.rmdir(container)
                    except OSError:
                        pass
            raise
        return record

    def trash_records(self):
        with self.write_lock:
            trash = self.trash_directory()
            records = []
            if trash is not None:
                for entry in self.client.listdir_attr(trash):
                    if not re.fullmatch(r'[a-f0-9]{32}', entry.filename) or not stat.S_ISDIR(entry.st_mode or 0):
                        continue
                    container = self.trash_container(entry.filename)
                    try:
                        record = self.read_record(container, entry.filename)
                        try:
                            self.client.lstat(posixpath.join(container, 'data'))
                            record['recoverable'] = True
                        except OSError as error:
                            if error.errno != errno.ENOENT:
                                raise
                            record['recoverable'] = False
                    except (OSError, ValueError, UnicodeError):
                        record = {'id': entry.filename, 'path': '损坏或不完整的回收站记录：' + entry.filename,
                                  'name': entry.filename, 'time': entry.st_mtime or 0,
                                  'operation': 'delete', 'recoverable': False}
                    records.append(record)
            records.sort(key=lambda item: item['time'], reverse=True)
            return {'entries': records, 'path': posixpath.join(self.home, TRASH_NAME)}

    def remove_tree(self, path):
        # Walk iteratively; unlink links themselves and refuse redirected ancestors.
        pending = [(path, False)]
        while pending:
            current, visited = pending.pop()
            parent = posixpath.dirname(current)
            if self.client.normalize(parent) != parent:
                raise ValueError('删除路径的父目录发生变化，操作已停止。')
            try:
                metadata = self.client.lstat(current)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    continue
                raise
            if stat.S_ISDIR(metadata.st_mode or 0):
                if self.client.normalize(current) != current:
                    raise ValueError('删除目录发生变化，操作已停止。')
                if visited:
                    self.client.rmdir(current)
                else:
                    children = self.client.listdir_attr(current)
                    if self.client.normalize(current) != current:
                        raise ValueError('删除目录发生变化，操作已停止。')
                    pending.append((current, True))
                    pending.extend((posixpath.join(current, name_value(child.filename)), False) for child in children)
            else:
                self.client.remove(current)

    def purge_record(self, identifier):
        container = self.trash_container(identifier)
        # Preserve metadata on failure, so a partially deleted record can be retried.
        self.remove_tree(posixpath.join(container, 'data'))
        for entry in self.client.listdir_attr(container):
            if entry.filename != 'record.json':
                self.remove_tree(posixpath.join(container, name_value(entry.filename)))
        self.remove_if_present(posixpath.join(container, 'record.json'))
        self.client.rmdir(container)

    def empty_trash(self):
        trash = self.trash_directory()
        deleted, errors = 0, []
        if trash is not None:
            for entry in self.client.listdir_attr(trash):
                if not re.fullmatch(r'[a-f0-9]{32}', entry.filename):
                    continue
                try:
                    self.purge_record(entry.filename)
                    deleted += 1
                except (OSError, ValueError, web.HTTPException):
                    errors.append({'id': entry.filename, 'error': '无法删除此记录，请检查远端权限或回收站路径后重试。'})
        return {'deleted': deleted, 'errors': errors}

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
        return web.json_response(await self.call(self.trash_records))
