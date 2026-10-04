"""Run with: python -m unittest -v test_server.py."""

import asyncio
import json
import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import server
from configuration import Settings


class HostDiscoveryTests(unittest.TestCase):
    def test_includes_aliases_patterns_duplicates_and_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').write_text(
                'Host prod backup *.example !excluded -bad\n'
                'HostName private.example\n'
                'Include = conf.d/*.conf\n'
                'Host=prod quoted\n'
            )
            (root / 'conf.d').mkdir()
            (root / 'conf.d/hosts.conf').write_text(
                'hOsT "jump" # a comment\n'
                'Host inner\nInclude config\n'
            )
            hosts = server.ssh_hosts(root / 'config')
            self.assertEqual([host['alias'] for host in hosts],
                             ['prod', 'backup', 'jump', 'inner', 'quoted'])
            self.assertEqual(set(hosts[0]), {'alias'})

    def test_missing_config_and_invalid_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            self.assertEqual(server.ssh_hosts(config), [])
            config.write_text('Host "unfinished\n')
            with self.assertRaises(ValueError):
                server.ssh_hosts(config)


class NetworkAccessTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, peer, local_address, host, origin=None, path='/api/info'):
        transport = Mock()
        def extra(name, default=None):
            return {'peername': (peer, 12345), 'sockname': (local_address, server.PORT)}.get(name, default)
        transport.get_extra_info.side_effect = extra
        headers = {'Host': host}
        if origin is not None:
            headers['Origin'] = origin
        request = make_mocked_request('GET', path, headers=headers, transport=transport)
        async def handler(request):
            return True
        return await server.access_control(request, handler)

    async def test_configured_peers_listeners_hosts_and_origins(self):
        ipv4 = '192.168.50.10'
        ipv6 = 'fd00::10'
        dns = 'ops.example.internal'
        settings = Settings(
            listen=('127.0.0.1', ipv4, ipv6), allowed_hosts=(ipv4, ipv6, dns),
            allowed_clients=(ipaddress.ip_network('127.0.0.0/8'),
                             ipaddress.ip_network('192.168.50.0/24'),
                             ipaddress.ip_network('fd00::/64')))
        with patch.object(server, 'CONFIG', settings):
            for peer, local, host in [('192.168.50.20', ipv4, ipv4),
                                      ('fd00::20', ipv6, '[' + ipv6 + ']'),
                                      ('192.168.50.20', ipv4, dns)]:
                authority = f'{host}:{server.PORT}'
                self.assertTrue(await self.check(peer, local, authority, f'http://{authority}', '/ws'))
            for peer, local, host, origin in [
                ('192.168.51.20', ipv4, ipv4, None),
                ('203.0.113.5', ipv4, ipv4, None),
                ('192.168.50.20', '192.168.50.11', ipv4, None),
                ('192.168.50.20', ipv4, 'evil.example', None),
                ('192.168.50.20', ipv4, ipv4, 'https://evil.example'),
            ]:
                with self.assertRaises(server.web.HTTPForbidden):
                    await self.check(peer, local, f'{host}:{server.PORT}', origin)
            with self.assertRaises(server.web.HTTPForbidden):
                await self.check('192.168.50.20', ipv4, f'{ipv4}:{server.PORT}', path='/ws')

    async def test_default_stays_loopback_only(self):
        with patch.object(server, 'CONFIG', Settings()):
            self.assertTrue(await self.check('127.0.0.1', '127.0.0.1', f'localhost:{server.PORT}'))
            with self.assertRaises(server.web.HTTPForbidden):
                await self.check('192.168.50.20', '192.168.50.10', f'192.168.50.10:{server.PORT}')

    async def test_wildcard_listener_still_checks_client_network(self):
        settings = Settings(listen=('0.0.0.0',), allowed_hosts=('ops.example.internal',),
                            allowed_clients=(ipaddress.ip_network('192.168.50.0/24'),))
        with patch.object(server, 'CONFIG', settings):
            self.assertTrue(await self.check('192.168.50.20', '192.168.50.10', f'ops.example.internal:{server.PORT}'))
            with self.assertRaises(server.web.HTTPForbidden):
                await self.check('192.168.51.20', '192.168.50.10', f'ops.example.internal:{server.PORT}')
            with self.assertRaises(server.web.HTTPForbidden):
                await self.check('192.168.50.20', 'fd00::10', f'ops.example.internal:{server.PORT}')


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = root / 'config'
        self.config.write_text('Host prod\n  HostName private.example\n')
        self.ssh = root / 'ssh'
        self.ssh.write_text(
            '#!/usr/bin/python3\n'
            'import json, os, sys\n'
            'print(json.dumps({"argv": sys.argv[1:], "tty": os.isatty(0)}), flush=True)\n'
            'print("Password:", flush=True)\n'
            'value = input()\n'
            'size = os.get_terminal_size()\n'
            'print(json.dumps({"received": value, "cols": size.columns, "rows": size.lines}), flush=True)\n'
        )
        self.ssh.chmod(0o700)
        self.config_patch = patch.object(server, 'SSH_CONFIG', self.config)
        self.ssh_patch = patch.object(server, 'SSH_EXECUTABLE', str(self.ssh))
        self.config_patch.start()
        self.ssh_patch.start()
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()
        self.port_patch = patch.object(server, 'PORT', self.client.port)
        self.port_patch.start()
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()
        self.port_patch.stop()
        self.ssh_patch.stop()
        self.config_patch.stop()
        self.directory.cleanup()
        self.assertFalse(server.SESSIONS)

    async def read_until(self, ws, marker):
        output = b''
        async with asyncio.timeout(5):
            while marker not in output:
                message = await ws.receive()
                if message.type == WSMsgType.BINARY:
                    output += message.data
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    self.fail('Terminal closed before expected output: ' + repr(output))
        return output

    async def test_hosts_refresh_without_exposing_connection_settings(self):
        response = await self.client.get('/api/hosts')
        self.assertEqual(await response.json(),
                         {'hosts': [{'alias': 'prod'}], 'ssh_available': True})
        self.config.write_text('Host changed\n')
        response = await self.client.get('/api/hosts')
        self.assertEqual((await response.json())['hosts'], [{'alias': 'changed'}])

    async def test_unknown_targets_and_options_cannot_open_terminal(self):
        for host in ['unknown', '-oProxyCommand=touch /tmp/pwned', 'prod;id', 'prod\nother']:
            response = await self.client.get('/ws', params={'host': host}, headers={'Origin': self.origin})
            self.assertEqual(response.status, 400)
        self.assertFalse(server.SESSIONS)

    async def test_local_access_and_origin_checks(self):
        response = await self.client.get('/api/hosts', headers={'Host': 'evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/ws', headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/ws')
        self.assertEqual(response.status, 403)
        self.assertFalse(server.SESSIONS)

    async def test_unreadable_config_and_missing_ssh(self):
        self.config.write_text('Host "unfinished\n')
        response = await self.client.get('/api/hosts')
        self.assertEqual(response.status, 503)
        self.config.write_text('Host prod\n')
        with patch.object(server, 'SSH_EXECUTABLE', None):
            response = await self.client.get('/ws?host=prod', headers={'Origin': self.origin})
            self.assertEqual(response.status, 503)
        self.assertFalse(server.SESSIONS)

    async def test_remote_pty_arguments_password_input_resize_and_exit(self):
        async with self.client.ws_connect('/ws?host=prod', origin=self.origin) as ws:
            output = await self.read_until(ws, b'Password:')
            arguments = ['-F', str(self.config), '-tt', '--', 'prod']
            self.assertIn(json.dumps(arguments).encode(), output)
            self.assertIn(b'"tty": true', output)
            await ws.send_json({'type': 'resize', 'cols': 120, 'rows': 40})
            await ws.send_json({'type': 'input', 'data': 'test-answer\n'})
            output = await self.read_until(ws, b'"rows": 40')
            self.assertIn(b'"received": "test-answer"', output)
            self.assertIn(b'"cols": 120', output)
            async with asyncio.timeout(5):
                while True:
                    message = await ws.receive()
                    if message.type == WSMsgType.TEXT and json.loads(message.data).get('type') == 'exit':
                        break
                    self.assertNotIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))

    async def test_local_shell_and_shared_session_limit(self):
        with patch.object(server, 'MAX_SESSIONS', 1):
            async with self.client.ws_connect('/ws', origin=self.origin) as ws:
                response = await self.client.get('/ws?host=prod', headers={'Origin': self.origin})
                self.assertEqual(response.status, 503)
                await ws.send_json({'type': 'input', 'data': "printf '%s\\n' 'local-'\"verified\"\n"})
                await self.read_until(ws, b'local-verified')

    async def test_shutdown_closes_remote_socket_and_process(self):
        async with self.client.ws_connect('/ws?host=prod', origin=self.origin) as ws:
            await self.read_until(ws, b'Password:')
            session = next(iter(server.SESSIONS))
            process = Path('/proc') / str(session.pid)
            shutdown = asyncio.create_task(server.shutdown(self.client.app))
            async with asyncio.timeout(5):
                while (await ws.receive()).type != WSMsgType.CLOSE:
                    pass
                await shutdown
            self.assertFalse(process.exists())


if __name__ == '__main__':
    unittest.main()
