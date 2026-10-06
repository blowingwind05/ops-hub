"""Shared file API rules; filesystem and SFTP operations stay in their backends."""

import asyncio
import hashlib
import posixpath
import re
import stat
import time
import uuid
from urllib.parse import quote

from aiohttp import web

TEXT_LIMIT = 2 * 1024 * 1024
ENTRY_LIMIT = 10000
TRASH_NAME = '.local/share/ops-hub/trash'


def path_input(value, home):
    """Expand the home shorthand without resolving local or remote symlinks."""
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('无效的文件路径。')
    value = value or str(home)
    if value == '~' or value.startswith('~/'):
        value = str(home) + value[1:]
    if not posixpath.isabs(value):
        raise ValueError('请输入绝对路径，或使用 ~/ 开头的路径。')
    return value


def name_value(value):
    if (not isinstance(value, str) or not value or value in ('.', '..')
            or '/' in value or '\x00' in value):
        raise ValueError('请输入有效的文件名，名称不能包含 /。')
    return value


def metadata_revision(fields):
    # Backends supply their own fields: local stat and SFTP expose different data.
    return hashlib.sha256(repr(tuple(fields)).encode()).hexdigest()


def encode_text(text, limit):
    if not isinstance(text, str) or '\x00' in text:
        raise ValueError('内容必须是文本，不能包含空字节。')
    data = text.encode('utf-8')
    if len(data) > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data),
                                           text='文本编辑限制为 2 MiB。')
    return data


def file_kind(mode):
    if stat.S_ISDIR(mode):
        return 'directory'
    if stat.S_ISREG(mode):
        return 'file'
    if stat.S_ISLNK(mode):
        return 'symlink'
    return 'special'


def file_entry(name, path, metadata, revision, *, protected=False,
               target_directory=False, link_target=None):
    mode = metadata.st_mode or 0
    return {'name': name, 'path': str(path), 'kind': file_kind(mode),
            'target_directory': target_directory, 'link_target': link_target,
            'size': metadata.st_size, 'modified': metadata.st_mtime,
            'mode': stat.filemode(mode), 'revision': revision, 'protected': protected}


def listing_result(path, home, entries, truncated, *, text_limit, upload_limit,
                   capabilities=None):
    entries.sort(key=lambda item: (not (item['kind'] == 'directory' or item.get('target_directory')),
                                   item['name'].casefold()))
    result = {'path': str(path), 'parent': posixpath.dirname(str(path)), 'root': '/',
              'home': str(home), 'entries': entries, 'truncated': truncated,
              'text_limit': text_limit, 'upload_limit': upload_limit}
    if capabilities is not None:
        result['capabilities'] = capabilities
    return result


def valid_trash_id(identifier):
    return isinstance(identifier, str) and re.fullmatch(r'[a-f0-9]{32}', identifier) is not None


def validate_trash_id(identifier):
    if not valid_trash_id(identifier):
        raise ValueError('无效的回收站记录。')


def new_trash_record(path, operation):
    path = str(path)
    return {'id': uuid.uuid4().hex, 'path': path, 'name': posixpath.basename(path),
            'time': time.time(), 'operation': operation}


def download_headers(name):
    return {'Content-Type': 'application/octet-stream',
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')}


async def upload_part(request):
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != 'file' or not part.filename:
        raise ValueError('请选择要上传的文件。')
    name_value(part.filename)
    return part


async def receive_upload(part, stream, limit):
    """Receive bounded HTTP chunks into a backend-owned temporary binary file."""
    total = 0
    while chunk := await part.read_chunk(65536):
        total += len(chunk)
        if limit and total > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=total,
                                               text=f'单个文件上传限制为 {limit / (1024 * 1024):g} MiB。')
        await asyncio.to_thread(stream.write, chunk)
    return total
