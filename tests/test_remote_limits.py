from concurrent.futures import ThreadPoolExecutor
import io
import json
import time

from fastapi.testclient import TestClient
import pytest

import main
import remote_limits as limits
import remote_limits_probe as helper


@pytest.mark.parametrize('shell,expected', [
    ('/home/user/.local/bin/acp-wrapper /usr/bin/codex --yolo', ('codex', '/usr/bin/codex')),
    ('claude --acp', ('claude', 'claude')),
    ('qwen --acp', (None, None)),
    ('codex && echo nope', (None, None)),
    ('"unterminated', (None, None)),
])
def test_backend(shell, expected):
    assert limits.backend(shell) == expected


def test_normalization_rejects_invalid_or_expired_metadata():
    future = time.time() + 10000
    raw = {'weekly': {'used_percent': 0, 'resets_at': future},
           '5-hours': {'used_percent': 100, 'resets_at': future}}
    assert [w['used_percent'] for w in limits.normalize(raw, 'claude')] == [0, 100]
    assert len(limits.normalize(raw, 'codex')) == 1
    for used in [True, '5', -1, 101, float('nan'), float('inf'), None]:
        assert limits.normalize({'weekly': {'used_percent': used, 'resets_at': future}}, 'codex') == []
    for reset in [None, True, 0, 'invalid', '2028-01-01', float('inf')]:
        assert limits.normalize({'weekly': {'used_percent': 5, 'resets_at': reset}}, 'codex') == []
    assert limits.normalize([], 'claude') == []


def test_cache_concurrent_requests_config_changes_and_expiry(monkeypatch):
    config = {'agent.shell': 'codex'}
    calls = []
    monkeypatch.setattr(limits.Config, 'snapshot', lambda keys: dict(config))
    monkeypatch.setattr(limits, '_cached', None)
    monkeypatch.setattr(limits, '_cached_config', None)
    monkeypatch.setattr(limits, '_expires', 0)
    def probe(values):
        calls.append(dict(values))
        time.sleep(.01)
        return {'reachable': True, 'agent': 'codex', 'limits': []}
    monkeypatch.setattr(limits, 'probe', probe)
    with ThreadPoolExecutor(max_workers=5) as pool:
        snapshots = list(pool.map(lambda _: limits.status(), range(5)))
    assert len(calls) == 1
    snapshots[0]['limits'].append('mutation')
    assert limits.status()['limits'] == []
    config['agent.shell'] = 'claude'
    limits.status()
    assert len(calls) == 2
    monkeypatch.setattr(limits, '_expires', 0)
    limits.status()
    assert len(calls) == 3


class Channel:
    def __init__(self, output=b'{}'):
        self.output = output
        self.closed = False
    def settimeout(self, timeout): pass
    def exec_command(self, command): self.command = command
    def sendall(self, data): self.script = data
    def shutdown_write(self): pass
    def recv_ready(self): return bool(self.output)
    def recv(self, size):
        data, self.output = self.output, b''
        return data
    def recv_stderr_ready(self): return False
    def exit_status_ready(self): return True
    def recv_exit_status(self): return 0
    def close(self): self.closed = True


def fake_ssh(monkeypatch, channel, failure=False):
    class Client:
        closed = False
        def load_system_host_keys(self): pass
        def set_missing_host_key_policy(self, policy):
            assert isinstance(policy, limits.paramiko.RejectPolicy)
        def connect(self, **kwargs):
            self.auth = kwargs
            if failure: raise OSError('secret-password')
        def get_transport(self): return self
        def open_session(self, timeout): return channel
        def close(self): self.closed = True
    client = Client()
    monkeypatch.setattr(limits.paramiko, 'SSHClient', lambda: client)
    return client


def config():
    return {'remote_server.host': '[::1]:2222', 'remote_server.username': 'user',
            'remote_server.password': 'secret-password', 'remote_server.ssh_key': '/private/key',
            'agent.shell': '/home/user/.local/bin/acp-wrapper /usr/bin/codex --yolo'}


def test_probe_auth_limits_and_cleanup(monkeypatch):
    channel = Channel(json.dumps({'weekly': {'used_percent': 22, 'resets_at': time.time() + 5000}}).encode())
    client = fake_ssh(monkeypatch, channel)
    result = limits.probe(config())
    assert result['reachable'] and result['limits'][0]['used_percent'] == 22
    assert client.auth['password'] is None
    assert client.auth['hostname'] == '::1' and client.auth['port'] == 2222
    assert not client.auth['allow_agent'] and not client.auth['look_for_keys']
    assert channel.command == 'python3 - codex /usr/bin/codex'
    assert b'secret-password' not in channel.script
    assert channel.closed and client.closed


def test_unavailable_ssh_and_unavailable_quota_are_distinct(monkeypatch):
    channel = Channel(b'not JSON')
    client = fake_ssh(monkeypatch, channel)
    assert limits.probe(config()) == {'reachable': True, 'agent': 'codex', 'limits': []}
    assert client.closed and channel.closed
    client = fake_ssh(monkeypatch, Channel(), failure=True)
    assert limits.probe(config()) == {'reachable': False, 'agent': 'codex', 'limits': []}
    assert client.closed


def test_api_contains_only_public_metadata(monkeypatch):
    expected = {'reachable': False, 'agent': None, 'limits': []}
    monkeypatch.setattr(limits, 'status', lambda: expected)
    with TestClient(main.app) as client:
        response = client.get('/api/agent/status')
        assert response.json() == expected
        assert response.headers['cache-control'] == 'no-store'
        home = client.get('/').text
        assert 'remote-status.js' in home and 'remote-status.css' in home
        assert 'remote-status.js' not in client.get('/archive').text


def test_claude_credentials_stay_remote(tmp_path, monkeypatch):
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    (tmp_path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'private-token'}}))
    def urlopen(request, timeout):
        assert request.full_url == 'https://api.anthropic.com/api/oauth/usage'
        assert request.get_header('Authorization') == 'Bearer private-token'
        assert timeout == 12
        return io.BytesIO(json.dumps({'seven_day': {'utilization': 30, 'resets_at': '2028-01-01T00:00:00Z'},
                                     'five_hour': {'utilization': 0, 'resets_at': '2028-01-01T00:00:00Z'}}).encode())
    monkeypatch.setattr(helper.urllib.request, 'urlopen', urlopen)
    result = helper.claude_limits()
    assert result['weekly']['used_percent'] == 30
    assert result['5-hours']['used_percent'] == 0
    assert 'private-token' not in json.dumps(result)


def test_codex_reads_metadata_without_model_turn(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text('''#!/usr/bin/env python3
import sys,json
assert sys.argv[1:] == ['app-server']
for line in sys.stdin:
 m=json.loads(line)
 if m['method']=='initialized': continue
 assert m['method'] in ['initialize','account/rateLimits/read']
 result={} if m['id']==1 else {'rateLimits':{'primary':{'windowDurationMins':300,'usedPercent':88,'resetsAt':2000000000},'secondary':{'windowDurationMins':10080,'usedPercent':3,'resetsAt':2000000000}}}
 print(json.dumps({'id':m['id'],'result':result}),flush=True)
''')
    executable.chmod(0o700)
    assert helper.codex_limits(str(executable)) == {'weekly': {'used_percent': 3, 'resets_at': 2000000000}}


def test_claude_api_provider_has_no_subscription_limits(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'api-secret')
    assert helper.claude_limits() == {}
