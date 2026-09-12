import sys
import types

import pytest

from settings import settings as store
from settings.config import Config
from settings.validation import remote_server


@pytest.mark.parametrize('host, expected', [
    ('example.org', ('example.org', 22)),
    ('example.org:2222', ('example.org', 2222)),
    ('127.0.0.1', ('127.0.0.1', 22)),
    ('[::1]:2222', ('::1', 2222)),
    ('2001:db8::1', ('2001:db8::1', 22)),
])
def test_host_parser(host, expected):
    assert remote_server._split_host(host) == expected


@pytest.mark.parametrize('host', ['', 'host:0', 'host:65536', 'host:bad', '[::1', 'bad host'])
def test_invalid_host(host):
    with pytest.raises(ValueError):
        remote_server._split_host(host)


@pytest.mark.parametrize('value', [1.5, True, '1.5', -1, [], {}])
def test_integer_rejects_lossy_conversion(monkeypatch, value):
    monkeypatch.setattr(store, 'validate_section', lambda *args: [])
    section = {'key': 'sample', 'groups': [{'fields': [{'id': 'n', 'type': 'integer'}]}]}
    with pytest.raises(ValueError):
        store.validate_values(section, {'n': value})


def test_config_missing_default_is_none(monkeypatch, tmp_path):
    section = {'key': 'sample', 'groups': [{'fields': [
        {'id': 'text', 'type': 'string'}, {'id': 'n', 'type': 'integer'},
        {'id': 'flag', 'type': 'checkbox'}, {'id': 'default', 'type': 'integer', 'default': 12},
    ]}]}
    monkeypatch.setattr(store, 'load_sections', lambda: [section])
    monkeypatch.setattr(store, 'USER_SETTINGS_DIR', tmp_path)
    for name in ['text', 'n', 'flag']:
        assert Config.get('sample.' + name) is None
    assert Config.get('sample.default') == 12
    assert store.load_values(section)['flag'] is False
    store.save_values('sample', {'n': 0}, tmp_path)
    assert Config.get('sample.n') == 0


@pytest.mark.parametrize('mode', ['output', 'timeout', 'exception'])
def test_ssh_validation_drains_bounds_and_redacts(monkeypatch, mode):
    secret = 'private-secret-material'
    closed = []
    class Channel:
        output = 2
        error = 2

        def recv_ready(self):
            return self.output > 0

        def recv_stderr_ready(self):
            return self.error > 0

        def recv(self, size):
            self.output -= 1
            return secret.encode()

        def recv_stderr(self, size):
            self.error -= 1
            return secret.encode()

        def exit_status_ready(self):
            return mode == 'output' and self.output == self.error == 0

        def recv_exit_status(self):
            assert self.output == self.error == 0
            return 1

        def close(self):
            closed.append('channel')

    channel = Channel()
    class Client:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            assert kwargs['hostname'] == 'example.org'
            assert kwargs['password'] is None
            assert kwargs['key_filename'] == '/test/key'
            if mode == 'exception':
                raise OSError(secret)

        def exec_command(self, command, timeout):
            assert timeout == remote_server.COMMAND_TIMEOUT
            return None, types.SimpleNamespace(channel=channel), None

        def close(self):
            closed.append('client')

    monkeypatch.setitem(sys.modules, 'paramiko', types.SimpleNamespace(SSHClient=Client, RejectPolicy=object))
    clock = iter(range(100))
    monkeypatch.setattr(remote_server.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(remote_server.time, 'sleep', lambda _: None)
    result = remote_server.validate_connection({'host': 'example.org', 'password': secret, 'ssh_key': '/test/key'})
    assert secret not in result
    assert 'client' in closed
    if mode != 'exception':
        assert 'channel' in closed
    if mode == 'timeout':
        assert result == 'SSH test command timed out'
    if mode == 'output':
        assert result == 'SSH test command failed with exit code 1'
