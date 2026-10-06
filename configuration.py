"""Load and validate deployment settings from a TOML file."""

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class Settings:
    listen: tuple[str, ...] = ('127.0.0.1',)
    port: int = 8088
    allowed_hosts: tuple[str, ...] = ('localhost', '127.0.0.1')
    allowed_clients: tuple = (ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'))
    directory: Path = field(default_factory=Path.home)
    ssh_config: Path = field(default_factory=lambda: Path.home() / '.ssh/config')
    upload_limit: int = 0  # Bytes; zero means unlimited.


def string_list(value, name):
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f'{name} must be a non-empty list of strings.')
    return tuple(dict.fromkeys(item.strip() for item in value))


def hostname(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        name = value.lower().rstrip('.')
        if len(name) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?', label) for label in name.split('.')):
            raise ValueError('access.allowed_hosts must contain IP addresses or explicit DNS names, without ports.')
        return name


def configured_path(value, base, name):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError(f'{name} must be a path string.')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_settings(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        document = tomllib.load(stream)
    schema = {'server': {'listen', 'port'}, 'access': {'allowed_hosts', 'allowed_clients'},
              'terminal': {'directory', 'ssh_config'}, 'files': {'upload_limit_mib'}}
    for section, values in document.items():
        if section not in schema:
            raise ValueError(f'Unknown configuration section: {section}')
        if not isinstance(values, dict):
            raise ValueError(f'{section} must be a TOML table.')
        if set(values) - schema[section]:
            raise ValueError(f'Unknown settings in {section}: {", ".join(sorted(set(values) - schema[section]))}')
    defaults = Settings()
    server = document.get('server', {})
    access = document.get('access', {})
    terminal = document.get('terminal', {})
    files = document.get('files', {})
    upload_limit_mib = files.get('upload_limit_mib', 0)
    if type(upload_limit_mib) is not int or upload_limit_mib < 0:
        raise ValueError('files.upload_limit_mib must be a non-negative integer (0 means unlimited).')
    listen = tuple(str(ipaddress.ip_address(value)) for value in
                   string_list(server.get('listen', list(defaults.listen)), 'server.listen'))
    port = server.get('port', defaults.port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('server.port must be an integer between 1 and 65535.')
    hosts = tuple(dict.fromkeys(hostname(value) for value in
                               string_list(access.get('allowed_hosts', list(defaults.allowed_hosts)), 'access.allowed_hosts')))
    clients = tuple(ipaddress.ip_network(value, strict=False) for value in
                    string_list(access.get('allowed_clients', [str(network) for network in defaults.allowed_clients]), 'access.allowed_clients'))
    directory = configured_path(terminal.get('directory', str(defaults.directory)), path.parent, 'terminal.directory')
    ssh_config = configured_path(terminal.get('ssh_config', str(defaults.ssh_config)), path.parent, 'terminal.ssh_config')
    if not directory.is_dir():
        raise ValueError('terminal.directory must be an existing directory.')
    return Settings(listen=listen, port=port, allowed_hosts=hosts, allowed_clients=clients,
                    directory=directory, ssh_config=ssh_config,
                    upload_limit=upload_limit_mib * 1024 * 1024)


def listener_matches(address, listeners):
    for value in listeners:
        listener = ipaddress.ip_address(value)
        if listener == address or (listener.is_unspecified and listener.version == address.version):
            return True
    return False
