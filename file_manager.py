"""Local file management with backups, trash, and explicit permanent deletion."""

import asyncio
import ctypes
import errno
import functools
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

from aiohttp import web

from file_common import (
    ENTRY_LIMIT, TEXT_LIMIT, TRASH_NAME, download_headers, encode_text,
    file_entry, file_kind, listing_result, metadata_revision, name_value,
    new_trash_record, path_input, receive_upload, upload_part, valid_trash_id,
    validate_trash_id,
)

WRITE_LOCK = threading.RLock()


def fail(status, message):
    return web.json_response({'error': message}, status=status)


def endpoint(handler):
    @functools.wraps(handler)
    async def wrapped(request):
        if request.method not in ('GET', 'HEAD'):
            if (request.headers.get('Origin') != f'http://{request.host}'
                    or request.headers.get('X-Ops-Hub-Request') != '1'):
                return fail(403, '文件操作必须从当前页面发起。')
        try:
            return await handler(request)
        except web.HTTPException as error:
            return fail(error.status, error.text)
        except UnicodeError:
            return fail(415, '仅支持编辑 UTF-8 文本文件。')
        except (ValueError, TypeError, KeyError) as error:
            return fail(400, str(error) or '无效的文件操作。')
        except OSError as error:
            messages = {
                errno.ENOENT: (404, '文件或目录不存在，请刷新后重试。'),
                errno.EACCES: (403, '没有权限访问此文件或目录。'),
                errno.EPERM: (403, '没有权限执行此操作。'),
                errno.EEXIST: (409, '同名文件或目录已存在，请使用其他名称。'),
                errno.ENOTDIR: (400, '路径不是目录。'),
                errno.EISDIR: (400, '请选择普通文件。'),
                errno.ELOOP: (400, '不能直接编辑或下载符号链接，请打开其目标路径。'),
                errno.ENOSPC: (507, '磁盘空间不足。'),
                errno.EROFS: (403, '该文件系统只读。'),
                errno.EXDEV: (400, '此操作不能跨文件系统执行，源文件已保留。'),
                errno.ENAMETOOLONG: (400, '文件名或路径过长。'),
            }
            code, message = messages.get(error.errno, (400, '文件操作失败。'))
            return fail(code, message)
    return wrapped


def path_value(value, home, follow=False):
    value = path_input(value, home)
    path = Path(os.path.abspath(value))
    # Resolve ancestors, but leave the final link intact for rename/trash.
    checked = path.resolve() if follow else path.parent.resolve() / path.name
    home = Path(home).resolve()
    trash = home / TRASH_NAME
    if checked == trash or checked.is_relative_to(trash):
        raise web.HTTPForbidden(text='请通过回收站入口管理这些记录。')
    return checked


def revision(metadata):
    fields = (metadata.st_dev, metadata.st_ino, metadata.st_mode,
              metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
    return metadata_revision(fields)


def check_revision(path, expected):
    if not isinstance(expected, str) or not expected:
        raise ValueError('缺少文件版本，请刷新后重试。')
    metadata = path.lstat()
    if revision(metadata) != expected:
        raise web.HTTPConflict(text='文件已被其他程序修改，请刷新或重新打开后再操作。')
    return metadata


def regular_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('仅支持普通文件。')
        data = stream.read(TEXT_LIMIT + 1) if metadata.st_size <= TEXT_LIMIT else b''
        if metadata.st_size > TEXT_LIMIT or len(data) > TEXT_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=TEXT_LIMIT, actual_size=max(metadata.st_size, len(data)),
                                               text='文本编辑限制为 2 MiB，请下载后编辑。')
        if revision(metadata) != revision(os.fstat(stream.fileno())):
            raise web.HTTPConflict(text='读取时文件发生变化，请重新打开。')
    return data, metadata


def directory_listing(path, home, upload_limit=0):
    path = path_value(str(path), home, follow=True)
    entries = []
    truncated = False
    with os.scandir(path) as iterator:
        for entry in iterator:
            if len(entries) >= ENTRY_LIMIT:
                truncated = True
                break
            try:
                metadata = entry.stat(follow_symlinks=False)
                kind = file_kind(metadata.st_mode)
                try:
                    path_value(entry.path, home)
                    protected = False
                except web.HTTPForbidden:
                    protected = True
                entries.append(file_entry(
                    entry.name, path / entry.name, metadata, revision(metadata), protected=protected,
                    target_directory=entry.is_dir() if kind == 'symlink' else False,
                    link_target=os.readlink(entry.path) if kind == 'symlink' else None))
            except OSError:
                entries.append({'name': entry.name, 'path': str(path / entry.name), 'kind': 'unavailable',
                                'size': None, 'modified': None, 'mode': '—', 'revision': None, 'protected': True})
    return listing_result(path, home, entries, truncated,
                          text_limit=TEXT_LIMIT, upload_limit=upload_limit)


def rename_new(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    # RENAME_NOREPLACE: never replace an existing file, link, or directory.
    if libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def archive(path, home, operation):
    trash = Path(home) / TRASH_NAME
    # Refuse a redirected trash directory or ancestor.
    if trash.resolve() != trash:
        raise ValueError('回收站路径包含符号链接，无法执行此操作。')
    trash.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = new_trash_record(path, operation)
    container = trash / record['id']
    container.mkdir(mode=0o700)
    try:
        (container / 'record.json').write_text(json.dumps(record, ensure_ascii=True))
        if operation == 'edit':
            shutil.copy2(path, container / 'data', follow_symlinks=False)
        else:
            rename_new(path, container / 'data')
    except BaseException:
        # Only discard an incomplete record, never an archived user file.
        if not (container / 'data').exists() and not (container / 'data').is_symlink():
            (container / 'record.json').unlink(missing_ok=True)
            container.rmdir()
        raise
    return record


def trash_records(home):
    trash = Path(home) / TRASH_NAME
    if trash.resolve() != trash:
        raise ValueError('回收站路径包含符号链接。')
    records = []
    if trash.exists():
        for container in trash.iterdir():
            if not container.is_dir() or container.is_symlink():
                continue
            try:
                record = json.loads((container / 'record.json').read_text())
                if (container / 'data').exists() or (container / 'data').is_symlink():
                    records.append(record)
            except (OSError, ValueError):
                continue
    records.sort(key=lambda item: item['time'], reverse=True)
    return {'entries': records}


def write_text(path, text, home, expected=None, create=False):
    data = encode_text(text, TEXT_LIMIT)
    with WRITE_LOCK:
        metadata = None if create else check_revision(path, expected)
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise ValueError('仅支持编辑普通文件，请勿直接编辑符号链接。')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.ops-hub-write-', delete=False) as stream:
                temporary = Path(stream.name)
                if metadata is not None:
                    current = os.fstat(stream.fileno())
                    if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                        os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                stream.write(data)
                stream.flush()
                if metadata is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
                    for attribute in os.listxattr(path, follow_symlinks=False):
                        value = os.getxattr(path, attribute, follow_symlinks=False)
                        os.setxattr(stream.fileno(), attribute, value)
                os.fsync(stream.fileno())
            if create:
                os.link(temporary, path)
            else:
                check_revision(path, expected)
                archive(path, home, 'edit')
                check_revision(path, expected)
                os.replace(temporary, path)
            return {'path': str(path), 'revision': revision(path.lstat())}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def trash_container(identifier, home):
    validate_trash_id(identifier)
    container = Path(home) / TRASH_NAME / identifier
    if container.is_symlink() or container.resolve() != container:
        raise ValueError('回收站路径包含符号链接。')
    return container


def purge_record(identifier, home):
    container = trash_container(identifier, home)
    # Delete the data first, preserving the record if the deletion fails.
    data = container / 'data'
    try:
        metadata = data.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(data)
        else:
            data.unlink()
    # rmtree never follows archived symlinks, including links inside folders.
    shutil.rmtree(container)


def empty_trash(home):
    trash = Path(home) / TRASH_NAME
    if trash.resolve() != trash:
        raise ValueError('回收站路径包含符号链接。')
    deleted, errors = 0, []
    if trash.exists():
        # Use actual container names, not identifiers supplied by record.json.
        for container in list(trash.iterdir()):
            if not valid_trash_id(container.name):
                continue
            try:
                purge_record(container.name, home)
                deleted += 1
            except (OSError, ValueError):
                errors.append({'id': container.name, 'error': '无法删除此记录，请检查访问权限和回收站路径后重试。'})
    return {'deleted': deleted, 'errors': errors}


def mutate(payload, home):
    action = payload['action']
    with WRITE_LOCK:
        if action == 'empty_trash':
            return empty_trash(home)
        if action in ('restore', 'purge'):
            identifier = payload['id']
            container = trash_container(identifier, home)
            if action == 'purge':
                purge_record(identifier, home)
                return {'id': identifier}
            record = json.loads((container / 'record.json').read_text())
            target = path_value(payload.get('destination') or record['path'], home)
            rename_new(container / 'data', target)
            (container / 'record.json').unlink()
            container.rmdir()
            return {'path': str(target)}
        path = path_value(payload.get('path', ''), home)
        if action in ('mkdir', 'create'):
            destination = path_value(str(path / name_value(payload['name'])), home)
            if action == 'mkdir':
                destination.mkdir(mode=0o755)
                return {'path': str(destination)}
            return write_text(destination, payload.get('content', ''), home, create=True)
        if action == 'save':
            return write_text(path, payload['content'], home, payload.get('revision'))
        if action not in ('rename', 'move', 'delete'):
            raise ValueError('不支持此文件操作。')
        if path == Path('/') or path == Path(home) or (Path(home) / TRASH_NAME).is_relative_to(path):
            raise ValueError('不能移动、重命名或删除根目录、用户主目录和回收站所在目录。')
        metadata = check_revision(path, payload.get('revision'))
        if action == 'move':
            directory = path_value(payload['destination'], home, follow=True)
            if not stat.S_ISDIR(directory.stat().st_mode):
                raise ValueError('移动目标必须是文件夹。')
            if stat.S_ISDIR(metadata.st_mode) and (directory == path or directory.is_relative_to(path)):
                raise ValueError('不能将文件夹移入自身或其子目录。')
            destination = path_value(str(directory / path.name), home)
            if destination == path:
                raise ValueError('文件已位于目标目录。')
            rename_new(path, destination)
            return {'path': str(destination)}
        if action == 'rename':
            destination = path_value(str(path.parent / name_value(payload['name'])), home)
            rename_new(path, destination)
            return {'path': str(destination)}
        return archive(path, home, 'delete')


def register_file_routes(app, home, remote_factory=None, *, upload_limit=0):
    connections = asyncio.Semaphore(4)

    def target(operation):
        def decorate(handler):
            @endpoint
            @functools.wraps(handler)
            async def dispatch(request):
                host = request.query.get('host', '')
                if not host:
                    return await handler(request)
                if remote_factory is None:
                    raise ValueError('远程文件管理未配置。')
                remote = remote_factory(host)
                async with connections:
                    try:
                        # Finish connection creation before cleanup even if the browser leaves.
                        connecting = asyncio.create_task(asyncio.to_thread(remote.connect))
                        try:
                            await asyncio.shield(connecting)
                        except asyncio.CancelledError:
                            await connecting
                            raise
                        return await getattr(remote, operation)(request)
                    finally:
                        await asyncio.to_thread(remote.close)
            return dispatch
        return decorate

    @target('listing')
    async def listing(request):
        path = path_value(request.query.get('path', ''), home, follow=True)
        return web.json_response(await asyncio.to_thread(directory_listing, path, home, upload_limit))

    @target('text')
    async def text(request):
        path = path_value(request.query.get('path', ''), home)
        data, metadata = await asyncio.to_thread(regular_bytes, path)
        if b'\x00' in data:
            return fail(415, '该文件包含二进制内容，请使用下载功能。')
        return web.json_response({'path': str(path), 'content': data.decode('utf-8'), 'revision': revision(metadata)})

    @target('download')
    async def download(request):
        path = path_value(request.query.get('path', ''), home)
        if not stat.S_ISREG((await asyncio.to_thread(path.lstat)).st_mode):
            raise ValueError('仅支持下载普通文件，请打开符号链接的目标路径。')
        return web.FileResponse(path, headers=download_headers(path.name))

    @target('action')
    async def action(request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('无效的文件操作。')
        return web.json_response(await asyncio.to_thread(mutate, payload, home))

    @target('upload')
    async def upload(request):
        directory = path_value(request.query.get('path', ''), home, follow=True)
        if not directory.is_dir():
            raise ValueError('请选择上传目录。')
        part = await upload_part(request)
        destination = path_value(str(directory / part.filename), home)
        temporary = None
        try:
            descriptor, name = await asyncio.to_thread(tempfile.mkstemp, dir=directory, prefix='.ops-hub-upload-')
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as stream:
                total = await receive_upload(part, stream, upload_limit)
                await asyncio.to_thread(stream.flush)
                await asyncio.to_thread(os.fsync, stream.fileno())
            await asyncio.to_thread(os.link, temporary, destination)
            return web.json_response({'path': str(destination), 'size': total})
        finally:
            if temporary is not None:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)

    @target('trash')
    async def trash(request):
        return web.json_response(await asyncio.to_thread(trash_records, home))

    app.router.add_get('/api/files', listing)
    app.router.add_get('/api/files/text', text)
    app.router.add_get('/api/files/download', download)
    app.router.add_get('/api/files/trash', trash)
    app.router.add_post('/api/files/action', action)
    app.router.add_post('/api/files/upload', upload)
