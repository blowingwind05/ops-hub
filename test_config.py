"""Deployment configuration validation and path resolution."""

import ipaddress
from pathlib import Path
import tempfile
import unittest

from configuration import Settings, load_settings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'settings.toml'

    def tearDown(self):
        self.temporary.cleanup()

    def load(self, text):
        self.path.write_text(text)
        return load_settings(self.path)

    def test_defaults_and_published_example(self):
        settings = self.load('')
        self.assertEqual(settings.listen, ('127.0.0.1',))
        self.assertEqual(settings.port, 8088)
        self.assertEqual(settings.upload_limit, 0)
        self.assertEqual(settings.directory, Path.home().resolve())
        self.assertEqual(load_settings(Path(__file__).parent / 'config.example.toml'), settings)

    def test_generic_networks_wildcards_names_and_relative_paths(self):
        settings = self.load('''[server]
listen = ["0.0.0.0", "::"]
port = 9000
[access]
allowed_hosts = ["OPS.Example.internal.", "192.168.50.10", "fd00:0::10"]
allowed_clients = ["192.168.50.20/24", "fd00::/64"]
[terminal]
directory = "."
ssh_config = "ssh.conf"
''')
        self.assertEqual(settings.listen, ('0.0.0.0', '::'))
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.allowed_hosts, ('ops.example.internal', '192.168.50.10', 'fd00::10'))
        self.assertEqual(settings.allowed_clients[0], ipaddress.ip_network('192.168.50.0/24'))
        self.assertEqual(settings.directory, self.root)
        self.assertEqual(settings.ssh_config, self.root / 'ssh.conf')

    def test_home_expansion_and_missing_directory(self):
        self.assertEqual(self.load('[terminal]\ndirectory = "~"\n').directory, Path.home().resolve())
        with self.assertRaises(ValueError):
            self.load('[terminal]\ndirectory = "missing-directory"\n')

    def test_upload_limit_zero_is_unlimited_and_positive_values_are_mib(self):
        self.assertEqual(Settings().upload_limit, 0)
        self.assertEqual(self.load('[files]\nupload_limit_mib = 0\n').upload_limit, 0)
        self.assertEqual(self.load('[files]\nupload_limit_mib = 256\n').upload_limit, 256 * 1024 * 1024)
        self.assertEqual(self.load('[files]\nupload_limit_mib = 1\n').upload_limit, 1024 * 1024)

    def test_mistyped_keys_and_invalid_values_fail_at_startup(self):
        invalid = ['[network]\nlisten = ["127.0.0.1"]',
                   '[server]\nlistne = ["127.0.0.1"]',
                   '[server]\nlisten = "127.0.0.1"',
                   '[server]\nlisten = []',
                   '[server]\nlisten = ["not-an-ip"]',
                   '[server]\nport = true',
                   '[server]\nport = 0',
                   '[server]\nport = 65536',
                   '[access]\nallowed_hosts = ["*"]',
                   '[access]\nallowed_hosts = ["ops.example:8088"]',
                   '[access]\nallowed_hosts = ["https://ops.example"]',
                   '[access]\nallowed_clients = ["invalid-cidr"]',
                   '[access]\nallowed_clients = []',
                   '[terminal]\nssh_config = 42',
                   '[files]\nupload_limit_mib = -1',
                   '[files]\nupload_limit_mib = true',
                   '[files]\nupload_limit_mib = 1.5',
                   '[files]\nupload_limit_mib = "0"',
                   'invalid toml = [']
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_explicit_missing_file_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            load_settings(self.root / 'missing.toml')


if __name__ == '__main__':
    unittest.main()
