import asyncio
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from settings import http_proxy as settings
from settings import settings as store
from tests.test_http_proxy import config, upstream


@contextmanager
def running_proxy(monkeypatch, url):
    monkeypatch.setenv('APP_WEB_HOST', '127.0.0.1')
    monkeypatch.setenv('APP_WEB_PORT', '8000')
    with socket.socket() as free:
        free.bind(('127.0.0.1', 0))
        port = free.getsockname()[1]
    initial = config(url, port=port, timeout=5)
    settings.save_values({'proxies': [initial]}, store.USER_SETTINGS_DIR)
    with TestClient(main.app) as client:
        manager = main.app.state.http_proxies
        assert not manager.errors
        yield client, manager, initial, f'http://127.0.0.1:{port}'


async def ok_upstream(reader, writer):
    head = await reader.readuntil(b'\r\n\r\n')
    if b'transfer-encoding: chunked' in head.lower():
        while True:
            size = int((await reader.readline()).strip(), 16)
            if size == 0:
                await reader.readline()
                break
            await reader.readexactly(size + 2)
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
    await writer.drain()


def test_save_applies_live_methods_patterns_and_preserves_transport(monkeypatch):
    async def run():
        async with upstream(ok_upstream) as url:
            with running_proxy(monkeypatch, url) as (client, manager, initial, address):
                server, task, app, listener = manager.servers[0]
                pool = app.pool
                async with httpx.AsyncClient(trust_env=False) as browser:
                    assert (await browser.get(address + '/old')).status_code == 200
                    changed = {**initial, 'methods': ['POST'], 'patterns': [
                        {'operator': '=', 'pattern': '^/new'}, {'operator': '!', 'pattern': '^/new/private'}]}
                    response = await asyncio.to_thread(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [changed]}})
                    assert response.status_code == 200
                    assert response.json()['restart_required'] is False
                    assert (await browser.get(address + '/new')).status_code == 403
                    assert (await browser.post(address + '/old')).status_code == 403
                    assert (await browser.post(address + '/new/private')).status_code == 403
                    assert (await browser.post(address + '/new?x=%2f')).status_code == 200
                    assert (await browser.head(address + '/new')).content == b''
                    assert manager.servers[0] == (server, task, app, listener) and app.pool is pool
                    assert settings.load_values(store.USER_SETTINGS_DIR)['proxies'] == [changed]
                    # Upstream and timeout remain unchanged even in a mixed Save.
                    changed = {**initial, 'methods': ['HEAD'], 'remote_url': url + '/', 'timeout': 10}
                    response = await asyncio.to_thread(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [changed]}})
                    assert response.json()['restart_required'] is True
                    assert app.config['timeout'] == 5 and app.remote.geturl() == url
                    assert (await browser.post(address + '/new')).status_code == 403
                    assert (await browser.head(address + '/new')).status_code == 200
                    for methods in (['ALL'], []):
                        response = await asyncio.to_thread(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [{**initial, 'methods': methods}]}})
                        assert response.json()['restart_required'] is False
                        assert (await browser.get(address + '/old')).status_code == 200
                        assert (await browser.request('TRACE', address)).status_code == 403
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['format', 'availability', 'write'])
def test_failed_save_keeps_file_and_live_policy(monkeypatch, failure):
    monkeypatch.setattr(settings, 'check_availability', lambda _: None)
    with running_proxy(monkeypatch, 'http://example.test') as (client, manager, initial, address):
        app = manager.servers[0][2]
        before = app.policy
        path = store.USER_SETTINGS_DIR / 'http_proxy.json'
        original = path.read_bytes()
        changed = {**initial, 'methods': ['POST']}
        if failure == 'format':
            changed['patterns'] = [{'operator': '=', 'pattern': '['}]
        elif failure == 'availability':
            def unavailable(_):
                raise ValueError(['Proxy 1: remote URL is not reachable.'])
            monkeypatch.setattr(settings, 'check_availability', unavailable)
        else:
            def write_failed(*args):
                raise OSError('secret')
            monkeypatch.setattr(settings, 'save_values', write_failed)
        response = client.post('/api/settings/http_proxy', json={'values': {'proxies': [changed]}})
        assert response.status_code == (500 if failure == 'write' else 422)
        assert app.policy is before and path.read_bytes() == original


def test_removed_changed_port_and_reordered_blocks(monkeypatch):
    monkeypatch.setattr(settings, 'check_availability', lambda _: None)
    with running_proxy(monkeypatch, 'http://example.test') as (client, manager, initial, address):
        app = manager.servers[0][2]
        before = app.policy
        another = {**initial, 'port': 8001 if initial['port'] != 8001 else 8002, 'methods': ['POST']}
        for proxies in ([another], []):
            response = client.post('/api/settings/http_proxy', json={'values': {'proxies': proxies}})
            assert response.json()['restart_required'] is True
            assert app.policy is before and len(manager.servers) == 1
        changed = {**initial, 'methods': ['DELETE']}
        response = client.post('/api/settings/http_proxy', json={'values': {'proxies': [another, changed]}})
        assert response.json()['restart_required'] is True
        assert app.policy[0] == frozenset(['DELETE']) and len(manager.servers) == 1


def test_save_keeps_open_sse_stream(monkeypatch):
    async def run():
        release = asyncio.Event()
        async def handler(reader, writer):
            head = await reader.readuntil(b'\r\n\r\n')
            if b'/events ' not in head:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: a\n\n\r\n')
            await writer.drain()
            await release.wait()
            writer.write(b'9\r\ndata: b\n\n\r\n0\r\n\r\n')
            await writer.drain()
        async with upstream(handler) as url:
            with running_proxy(monkeypatch, url) as (client, manager, initial, address):
                async with httpx.AsyncClient(trust_env=False) as browser:
                    async with browser.stream('GET', address + '/events') as stream:
                        chunks = stream.aiter_raw()
                        assert await anext(chunks) == b'data: a\n\n'
                        changed = {**initial, 'methods': ['POST'], 'patterns': [{'operator': '!', 'pattern': '^/events'}]}
                        response = await asyncio.to_thread(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [changed]}})
                        assert response.json()['restart_required'] is False
                        assert (await browser.get(address + '/events')).status_code == 403
                        assert (await browser.post(address + '/events')).status_code == 403
                        release.set()
                        assert b''.join([chunk async for chunk in chunks]) == b'data: b\n\n'
    asyncio.run(run())


def test_concurrent_saves_keep_disk_and_policy_in_order(monkeypatch):
    monkeypatch.setattr(settings, 'check_availability', lambda _: None)
    with running_proxy(monkeypatch, 'http://example.test') as (client, manager, initial, address):
        entered = threading.Event()
        release = threading.Event()
        original_apply = manager.apply_policies
        def apply(values):
            if values['proxies'][0]['methods'] == ['POST']:
                entered.set()
                assert release.wait(5)
            return original_apply(values)
        monkeypatch.setattr(manager, 'apply_policies', apply)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [{**initial, 'methods': ['POST']}]}})
            assert entered.wait(5)
            second = executor.submit(client.post, '/api/settings/http_proxy', json={'values': {'proxies': [{**initial, 'methods': ['DELETE']}]}})
            try:
                assert not second.done()
            finally:
                release.set()
            assert first.result().status_code == second.result().status_code == 200
        saved = json.loads((store.USER_SETTINGS_DIR / 'http_proxy.json').read_text())
        assert saved['proxies'][0]['methods'] == ['DELETE']
        assert manager.servers[0][2].policy[0] == frozenset(['DELETE'])
