import json
import sys
from unittest.mock import patch

import httpx
import pytest

from settings import http_proxy, settings
from settings.config import Config


def valid():
    return {'proxies': [{'port': '8001', 'timeout': '30', 'remote_url': 'https://example.com/',
                         'methods': ['GET', 'ALL'], 'patterns': [{'operator': '=', 'pattern': ' ^/x'}]}]}


def test_validate_normalizes_and_all():
    out = http_proxy.validate_values(valid(), 8000)
    assert out['proxies'][0]['port'] == 8001
    assert out['proxies'][0]['methods'] == ['ALL']
    assert out['proxies'][0]['patterns'][0]['pattern'] == ' ^/x'


@pytest.mark.parametrize('field,value', [
    ('port', True), ('port', 8001.0), ('port', 1.2), ('port', []), ('port', {}),
    ('port', '8001.0'), ('port', ''), ('port', 8000), ('port', 65536),
    ('timeout', []), ('timeout', False), ('timeout', 1.2), ('timeout', ''), ('timeout', 0), ('timeout', -1),
    ('remote_url', 'https://x/a'), ('remote_url', 'https://u:p@x/'), ('remote_url', 'https://x?'),
    ('remote_url', 'https://x/#'), ('remote_url', 'http://'), ('remote_url', 'ftp://x'),
    ('remote_url', 'https://x:65536/'), ('remote_url', 'https://x:0/'), ('remote_url', 'https://x:abc/'),
    ('remote_url', 'https://x:/'), ('remote_url', 'http://bad host/'), ('remote_url', 'http://x\n/'),
    ('remote_url', 'http://x\\@evil/'), ('remote_url', 'http://x/?a=b'), ('remote_url', 'http://x/#fragment'),
    ('methods', ['TRACE']), ('methods', ['']), ('methods', 'ALL'), ('methods', [None]),
    ('patterns', [{'operator': '=', 'pattern': '['}]), ('patterns', [{'operator': '-', 'pattern': ''}]),
    ('patterns', [{'operator': '=', 'pattern': None}]), ('patterns', {}),
])
def test_rejects_invalid(field, value):
    values = valid()
    values['proxies'][0][field] = value
    with pytest.raises(ValueError) as error:
        http_proxy.validate_values(values)
    assert isinstance(error.value.args[0], list)
    assert error.value.args[0][0].startswith('Proxy 1: ')


@pytest.mark.parametrize('values', [{}, [], {'proxies': None}, {'proxies': {}}, {'proxies': [True]}])
def test_invalid_structure(values):
    with pytest.raises(ValueError):
        http_proxy.validate_values(values)


def test_main_port_duplicates_empty_patterns_and_defaults():
    values = valid()
    with pytest.raises(ValueError, match='application'):
        http_proxy.validate_values(values, main_port=8001)
    values['proxies'].append(dict(values['proxies'][0]))
    with pytest.raises(ValueError, match='unique'):
        http_proxy.validate_values(values)
    values['proxies'].pop()
    proxy = values['proxies'][0]
    del proxy['timeout']
    proxy['patterns'] = [{'operator': '=', 'pattern': ''}, {'operator': '!', 'pattern': ' '}]
    proxy['methods'] = []
    proxy['port'] = '+08001'
    normalized = http_proxy.validate_values(values)['proxies'][0]
    assert normalized['timeout'] == 30 and normalized['port'] == 8001
    assert normalized['methods'] == [] and normalized['patterns'] == [{'operator': '!', 'pattern': ' '}]


@pytest.mark.parametrize('url', ['http://localhost', 'https://example.com/', 'http://[::1]:8090/', 'http://example.test:65535'])
def test_valid_origins(url):
    values = valid()
    values['proxies'][0]['remote_url'] = url
    assert http_proxy.validate_values(values)['proxies'][0]['remote_url'] == url


def test_check_stream_policy():
    values = http_proxy.validate_values(valid())
    with patch('settings.http_proxy.httpx.Client') as client_class:
        client = client_class.return_value.__enter__.return_value
        response = client.stream.return_value.__enter__.return_value
        response.status_code = 503
        http_proxy.check_availability(values)
        assert client_class.call_args.kwargs == {'verify': False, 'trust_env': False, 'follow_redirects': False, 'timeout': 30}
        client.stream.assert_called_once_with('GET', 'https://example.com/')
        response.read.assert_not_called()
        client.stream.return_value.__exit__.assert_called_once()


def test_check_errors_safe_and_all_blocks_checked():
    values = valid()
    values['proxies'].append({**values['proxies'][0], 'port': 8002})
    values = http_proxy.validate_values(values)
    with patch('settings.http_proxy.httpx.Client', side_effect=httpx.ConnectError('secret internal DNS details')) as client:
        with pytest.raises(ValueError) as error:
            http_proxy.check_availability(values)
        assert error.value.args[0] == ['Proxy 1: remote URL is not reachable.', 'Proxy 2: remote URL is not reachable.']
        assert client.call_count == 2
    with patch('settings.http_proxy.httpx.Client') as client:
        http_proxy.check_availability({'proxies': []})
        client.assert_not_called()


def test_atomic_save_and_dispatch(tmp_path, monkeypatch):
    values = http_proxy.validate_values(valid())
    monkeypatch.setattr(settings, 'USER_SETTINGS_DIR', tmp_path)
    assert Config.get('http_proxy.proxies') == []
    http_proxy.save_values(values, tmp_path)
    assert json.loads((tmp_path / 'http_proxy.json').read_text()) == values
    assert settings.load_values({'key': 'http_proxy'}, tmp_path) == values
    assert Config.get('http_proxy.proxies') == values['proxies']
    assert settings.load_sections()[-1]['key'] == 'http_proxy'
    with patch('settings.http_proxy.os.replace', side_effect=OSError('disk error')):
        with pytest.raises(OSError):
            http_proxy.save_values({'proxies': []}, tmp_path)
    assert http_proxy.load_values(tmp_path) == values
    assert list(tmp_path.glob('.http_proxy.*')) == []


def test_bad_entry_preserved_for_visible_start_error(tmp_path):
    values = {'proxies': [None, valid()['proxies'][0]]}
    (tmp_path / 'http_proxy.json').write_text(json.dumps(values))
    assert http_proxy.load_values(tmp_path) == values


def test_application_address(monkeypatch):
    monkeypatch.delenv('APP_WEB_HOST', raising=False)
    monkeypatch.delenv('APP_WEB_PORT', raising=False)
    monkeypatch.setattr(sys, 'argv', ['pytest'])
    assert http_proxy.application_address() == ('127.0.0.1', None)
    monkeypatch.setattr(sys, 'argv', ['/path/uvicorn/__main__.py', 'main:app', '--host', '::', '--port=9000'])
    assert http_proxy.application_address() == ('::', 9000)
    monkeypatch.setenv('APP_WEB_HOST', '0.0.0.0')
    monkeypatch.setenv('APP_WEB_PORT', '9001')
    assert http_proxy.application_address() == ('0.0.0.0', 9001)
