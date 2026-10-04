"""Local and SSH browser terminals backed by real Linux PTYs."""

import asyncio
import fcntl
import glob
import ipaddress
import json
import os
import pty
import pwd
import re
import shlex
import shutil
import signal
import socket
import struct
import termios
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent
PORT = 8088
HOME_DIR = Path('/home/vlab')
SHELL = pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'
MAX_SESSIONS = 8
SESSIONS = set()
SSH_CONFIG = HOME_DIR / '.ssh/config'
SSH_EXECUTABLE = shutil.which('ssh')


def ssh_hosts(config=None):
    """Discover literal aliases; OpenSSH resolves all connection settings."""
    config = Path(config) if config is not None else SSH_CONFIG
    aliases = {}
    visited = set()

    def read(path, depth=0):
        path = path.resolve()
        if path in visited or depth > 16:
            return
        visited.add(path)
        for line in path.read_text().splitlines():
            match = re.match(r'^\s*([^\s=]+)(?:\s*=\s*|\s+)(.*)$', line)
            if not match:
                continue
            keyword, value = match.groups()
            try:
                values = shlex.split(value, comments=True)
            except ValueError as error:
                raise ValueError('Invalid SSH configuration syntax.') from error
            if keyword.lower() == 'host':
                for alias in values:
                    if (not alias.startswith(('-', '!'))
                            and not any(char in alias for char in '*?')
                            and re.fullmatch(r'[A-Za-z0-9_.:@+\-]+', alias)):
                        aliases.setdefault(alias, {'alias': alias})
            elif keyword.lower() == 'include':
                for pattern in values:
                    expanded = Path(os.path.expanduser(pattern))
                    if not expanded.is_absolute():
                        expanded = config.parent / expanded
                    for included in sorted(glob.glob(str(expanded))):
                        read(Path(included), depth + 1)

    if config.exists():
        read(config)
    return list(aliases.values())


def configured_hosts():
    try:
        return ssh_hosts()
    except (OSError, ValueError, RuntimeError) as error:
        raise web.HTTPServiceUnavailable(text='SSH 配置无法读取，请检查 ~/.ssh/config。') from error


@web.middleware
async def local_only(request, handler):
    """Reject cross-origin requests and DNS rebinding before opening a PTY."""
    try:
        local_peer = ipaddress.ip_address(request.remote).is_loopback
    except (ValueError, TypeError):
        local_peer = False
    allowed_hosts = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}
    if not local_peer or request.host not in allowed_hosts:
        raise web.HTTPForbidden(text='Only local access is supported.')
    origin = request.headers.get('Origin')
    if origin and origin != f'http://{request.host}':
        raise web.HTTPForbidden(text='Cross-origin access is not allowed.')
    if request.path == '/ws' and origin != f'http://{request.host}':
        raise web.HTTPForbidden(text='A same-origin browser session is required.')
    return await handler(request)


async def response_headers(request, response):
    response.headers.update({
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Cross-Origin-Resource-Policy': 'same-origin',
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
    })


class TerminalSession:
    def __init__(self, cols, rows, host=None):
        self.host = host
        executable = SSH_EXECUTABLE if host else SHELL
        arguments = [executable, '-tt', '--', host] if host else [Path(SHELL).name, '-l']
        self.loop = asyncio.get_running_loop()
        self.output = asyncio.Queue(maxsize=128)
        self.paused = False
        self.closed = False
        self.close_done = asyncio.Event()
        self.ws = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(HOME_DIR)
                environment = os.environ.copy()
                environment.update(TERM='xterm-256color', COLORTERM='truecolor')
                environment.pop('PYTHONPATH', None)
                environment.pop('VIRTUAL_ENV', None)
                os.execve(executable, arguments, environment)
            except BaseException:
                os.write(2, b'Unable to start terminal process.\r\n')
                os._exit(127)
        os.set_blocking(self.fd, False)
        self.resize(cols, rows)
        self.loop.add_reader(self.fd, self.read_ready)

    def resize(self, cols, rows):
        cols = max(20, min(500, int(cols)))
        rows = max(5, min(300, int(rows)))
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def read_ready(self):
        if self.output.full():
            self.loop.remove_reader(self.fd)
            self.paused = True
            return
        try:
            data = os.read(self.fd, 16384)
        except BlockingIOError:
            return
        except OSError:
            data = b''
        self.output.put_nowait(data)
        if not data:
            self.loop.remove_reader(self.fd)

    def foreground_program(self):
        try:
            group = os.tcgetpgrp(self.fd)
            if group > 0:
                return Path(f'/proc/{group}/comm').read_text().strip() or Path(SHELL).name
        except (OSError, ValueError):
            pass
        return 'ssh' if self.host else Path(SHELL).name

    async def send_output(self, ws):
        last_program = None
        next_process_check = 0
        while not self.closed:
            now = self.loop.time()
            if now >= next_process_check:
                program = self.foreground_program()
                if program != last_program:
                    await ws.send_json({'type': 'process', 'name': program})
                    last_program = program
                next_process_check = now + 0.5
            try:
                data = await asyncio.wait_for(self.output.get(), timeout=max(0.01, next_process_check - self.loop.time()))
            except asyncio.TimeoutError:
                continue
            if self.paused:
                self.paused = False
                self.loop.add_reader(self.fd, self.read_ready)
            if not data:
                await ws.send_json({'type': 'exit'})
                await ws.close()
                return
            await ws.send_bytes(data)

    async def write(self, text):
        data = memoryview(text.encode('utf-8'))
        while data and not self.closed:
            try:
                written = os.write(self.fd, data)
                data = data[written:]
            except BlockingIOError:
                ready = self.loop.create_future()
                def writable():
                    if not ready.done():
                        ready.set_result(None)
                self.loop.add_writer(self.fd, writable)
                try:
                    await asyncio.wait_for(ready, timeout=10)
                finally:
                    self.loop.remove_writer(self.fd)

    async def close(self):
        if self.closed:
            await self.close_done.wait()
            return
        self.closed = True
        try:
            await self.close_process()
        finally:
            self.close_done.set()

    async def close_process(self):
        self.loop.remove_reader(self.fd)
        self.loop.remove_writer(self.fd)
        os.close(self.fd)
        # A page disconnect ends its shell and any foreground terminal job.
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        for _ in range(20):
            try:
                reaped, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return
            if reaped:
                return
            await asyncio.sleep(0.05)
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.to_thread(os.waitpid, self.pid, 0)
        except ChildProcessError:
            pass


async def terminal(request):
    if len(SESSIONS) >= MAX_SESSIONS:
        raise web.HTTPServiceUnavailable(text='Too many terminal sessions.')
    try:
        cols = int(request.query.get('cols', 100))
        rows = int(request.query.get('rows', 28))
    except ValueError:
        raise web.HTTPBadRequest(text='Invalid terminal size.')
    host = request.query.get('host') or None
    if host:
        if host not in {entry['alias'] for entry in configured_hosts()}:
            raise web.HTTPBadRequest(text='请选择 SSH 配置中的服务器。')
        if not SSH_EXECUTABLE:
            raise web.HTTPServiceUnavailable(text='本机未安装 SSH 客户端。')
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=131072)
    if not ws.can_prepare(request).ok:
        raise web.HTTPBadRequest(text='A WebSocket connection is required.')
    # Reserve the slot before the handshake yields to other connections.
    session = TerminalSession(cols, rows, host)
    session.ws = ws
    SESSIONS.add(session)
    sender = None
    try:
        await ws.prepare(request)
        sender = asyncio.create_task(session.send_output(ws))
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict):
                        raise ValueError('Invalid message')
                    if payload.get('type') == 'input':
                        data = payload.get('data')
                        if not isinstance(data, str):
                            raise ValueError('Invalid input')
                        await session.write(data)
                    elif payload.get('type') == 'resize':
                        session.resize(payload['cols'], payload['rows'])
                except (ValueError, KeyError, TypeError, OSError, asyncio.TimeoutError):
                    await ws.close(code=1008, message=b'Invalid terminal message')
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        try:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        finally:
            await session.close()
            SESSIONS.discard(session)
    return ws


async def info(request):
    return web.json_response({
        'hostname': socket.gethostname(),
        'username': pwd.getpwuid(os.getuid()).pw_name,
        'directory': str(HOME_DIR),
        'shell': SHELL,
    })


async def hosts(request):
    return web.json_response({'hosts': configured_hosts(), 'ssh_available': bool(SSH_EXECUTABLE)})


async def index(request):
    return web.FileResponse(ROOT / 'static/index.html')


async def shutdown(app):
    sessions = tuple(SESSIONS)
    await asyncio.gather(*(session.ws.close(code=1001, message=b'Service restarting')
                           for session in sessions if session.ws is not None))
    await asyncio.gather(*(session.close() for session in sessions))


def create_app():
    # WebSocket disconnects end the receive loop. Do not also cancel the
    # handler on transport loss: cancellation can interrupt shell cleanup.
    app = web.Application(middlewares=[local_only])
    app.on_response_prepare.append(response_headers)
    app.on_shutdown.append(shutdown)
    app.router.add_get('/', index)
    app.router.add_get('/api/info', info)
    app.router.add_get('/api/hosts', hosts)
    app.router.add_get('/ws', terminal)
    app.router.add_static('/static/', ROOT / 'static', show_index=False)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=PORT, access_log=None, print=None)
