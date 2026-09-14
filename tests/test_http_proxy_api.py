import json
import socket

from fastapi.testclient import TestClient

import main
from settings import http_proxy
from settings import settings as store


def proxy(port=18081):
    return {'port': port, 'timeout': 30, 'remote_url': 'http://example.test/',
            'methods': ['ALL'], 'patterns': []}


def test_proxy_api_atomic_validation_restart_and_empty(monkeypatch):
    monkeypatch.setenv('APP_WEB_HOST', '127.0.0.1')
    monkeypatch.setenv('APP_WEB_PORT', '18080')
    checked = []
    monkeypatch.setattr(http_proxy, 'check_availability', lambda values: checked.append(values))
    with TestClient(main.app) as client:
        manager = main.app.state.http_proxies
        result = client.post('/api/settings/http_proxy', json={'values': {'proxies': [proxy()]}})
        assert result.status_code == 200
        path = store.USER_SETTINGS_DIR / 'http_proxy.json'
        original = path.read_bytes()
        assert checked == [result.json()['values']]
        assert manager.servers == []
        conflict = client.post('/api/settings/http_proxy', json={'values': {'proxies': [proxy(18080)]}})
        assert conflict.status_code == 422
        assert path.read_bytes() == original
        assert len(checked) == 1

        def fail(values):
            raise ValueError(['Proxy 1: remote URL is not reachable.'])

        monkeypatch.setattr(http_proxy, 'check_availability', fail)
        assert client.post('/api/settings/http_proxy', json={'values': {'proxies': [proxy()]}}).status_code == 422
        assert path.read_bytes() == original
        monkeypatch.setattr(http_proxy, 'check_availability', lambda values: None)
        empty = client.post('/api/settings/http_proxy', json={'values': {'proxies': []}})
        assert empty.status_code == 200
        assert json.loads(path.read_text()) == {'proxies': []}


def test_proxy_start_failure_visible_and_application_survives(monkeypatch):
    monkeypatch.setenv('APP_WEB_HOST', '127.0.0.1')
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        store.USER_SETTINGS_DIR.mkdir(parents=True)
        (store.USER_SETTINGS_DIR / 'http_proxy.json').write_text(json.dumps({'proxies': [proxy(port)]}))
        with TestClient(main.app) as client:
            assert client.get('/health').status_code == 200
            page = client.get('/settings?section=http_proxy')
            assert page.status_code == 200
            assert f'Proxy 1 (port {port})' in page.text
            assert 'could not start' in page.text
            assert main.app.state.http_proxies.servers == []


def test_proxy_write_error_is_safe(monkeypatch):
    def fail(*args):
        raise OSError('sensitive filesystem details')

    monkeypatch.setattr(http_proxy, 'save_values', fail)
    with TestClient(main.app) as client:
        result = client.post('/api/settings/http_proxy', json={'values': {'proxies': []}})
        assert result.status_code == 500
        assert result.json()['detail'] == 'HTTP proxy: could not save configuration.'


def test_lifespan_snapshot_changes_only_after_restart(monkeypatch):
    monkeypatch.setenv('APP_WEB_HOST', '127.0.0.1')
    monkeypatch.setenv('APP_WEB_PORT', '8000')
    monkeypatch.setattr(http_proxy, 'check_availability', lambda values: None)
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    store.USER_SETTINGS_DIR.mkdir(parents=True)
    path = store.USER_SETTINGS_DIR / 'http_proxy.json'
    path.write_text(json.dumps({'proxies': [proxy(port)]}))
    with TestClient(main.app) as client:
        manager = main.app.state.http_proxies
        assert len(manager.servers) == 1
        assert client.post('/api/settings/http_proxy', json={'values': {'proxies': []}}).status_code == 200
        assert len(manager.servers) == 1
        with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
            connection.sendall(b'TRACE / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
            assert b'403' in connection.recv(1024)
    assert manager.servers == []
    with socket.socket() as released:
        released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        released.bind(('127.0.0.1', port))
    with TestClient(main.app):
        assert main.app.state.http_proxies.servers == []
