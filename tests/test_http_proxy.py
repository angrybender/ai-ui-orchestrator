import asyncio
import gzip
import socket
from contextlib import asynccontextmanager

import httpcore
import pytest

from http_proxy import ProxyApplication, ProxyServers


def config(url, **kwargs):
    return {'port': 8001, 'timeout': 1, 'remote_url': url, 'methods': ['ALL'], 'patterns': [], **kwargs}


@asynccontextmanager
async def upstream(handler):
    tasks = set()

    async def run(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(run, '127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def request(app, method='GET', target=b'/', body=b'', headers=(), disconnect=None):
    sent = []
    received = False
    done = asyncio.Event()

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await (disconnect or done).wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    path, _, query = target.partition(b'?')
    await app({'type': 'http', 'method': method, 'raw_path': path, 'query_string': query,
               'headers': list(headers)}, receive, send)
    return sent


async def read_request(reader):
    head = await reader.readuntil(b'\r\n\r\n')
    body = b''
    while True:
        size = int((await reader.readline()).strip(), 16)
        if size == 0:
            await reader.readline()
            break
        body += await reader.readexactly(size)
        await reader.readexactly(2)
    return head, body


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def test_raw_uri_method_body_and_headers(method):
    async def run():
        seen = []
        async def handler(reader, writer):
            seen.append(await read_request(reader))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
        async with upstream(handler) as url:
            app = ProxyApplication(config(url))
            try:
                result = await request(app, method, b'/a/../%2e%2e//x?x=%2f&x=+', b'payload',
                    [(b'host', b'client'), (b'cookie', b'a=1'), (b'authorization', b'Bearer test'),
                     (b'connection', b'x-private'), (b'x-private', b'secret'), (b'x-repeat', b'1'), (b'x-repeat', b'2')])
                assert result[0]['status'] == 200
                head, body = seen[0]
                assert head.startswith(method.encode() + b' /a/../%2e%2e//x?x=%2f&x=+ HTTP/1.1')
                assert body == b'payload'
                assert b'host: ' + url.removeprefix('http://').encode() in head
                assert b'cookie: a=1' in head and b'authorization: Bearer test' in head
                assert b'x-private' not in head and head.count(b'x-repeat:') == 2
            finally:
                await app.close()
    asyncio.run(run())


@pytest.mark.parametrize('method,target,methods,patterns,allowed', [
    ('TRACE', b'/', ['ALL'], [], False), ('CONNECT', b'/', [], [], False),
    ('CUSTOM', b'/', ['ALL'], [], False), ('POST', b'/', ['GET'], [], False),
    ('HEAD', b'/', ['GET'], [], False), ('GET', b'/documents/12', [], [('=', r'^/documents/[\d]+$')], True),
    ('GET', b'/documents/12?id=1', ['ALL'], [('=', r'^/documents/[\d]+$')], False),
    ('GET', b'/documents/private/', ['ALL'], [('=', '^/documents/'), ('!', '^/documents/private/')], False),
    ('GET', b'/documents/%31', ['GET', 'POST'], [('=', '^/documents/%31$')], True),
])
def test_method_and_pattern_filters(method, target, methods, patterns, allowed):
    async def run():
        hits = []
        async def handler(reader, writer):
            hits.append(await read_request(reader))
            writer.write(b'HTTP/1.1 204 No Content\r\n\r\n')
            await writer.drain()
        async with upstream(handler) as url:
            app = ProxyApplication(config(url, methods=methods, patterns=[{'operator': op, 'pattern': p} for op, p in patterns]))
            try:
                result = await request(app, method, target)
                assert result[0]['status'] == (204 if allowed else 403)
                assert bool(hits) == allowed
                if not allowed:
                    assert result[1]['body'] == (b'' if method == 'HEAD' else b'Access denied by proxy server')
            finally:
                await app.close()
    asyncio.run(run())


def test_raw_compression_duplicates_redirect_and_cookie_isolation():
    async def run():
        body = gzip.compress(b'compressed payload')
        seen = []
        async def handler(reader, writer):
            seen.append(await read_request(reader))
            writer.write(b'HTTP/1.1 302 Found\r\nLocation: https://elsewhere.test/path\r\n'
                         b'Set-Cookie: a=1; Domain=origin.test; Path=/abc\r\nSet-Cookie: b=2\r\n'
                         b'Content-Encoding: gzip\r\nConnection: close, x-secret\r\nX-Secret: removed\r\n'
                         b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        async with upstream(handler) as url:
            app = ProxyApplication(config(url))
            try:
                for _ in range(2):
                    result = await request(app)
                    assert result[0]['status'] == 302
                    headers = result[0]['headers']
                    assert [v for k, v in headers if k == b'set-cookie'] == [b'a=1; Domain=origin.test; Path=/abc', b'b=2']
                    assert (b'location', b'https://elsewhere.test/path') in headers
                    assert (b'content-encoding', b'gzip') in headers
                    assert not any(k in (b'connection', b'x-secret') for k, _ in headers)
                    assert b''.join(m.get('body', b'') for m in result) == body
                assert len(seen) == 2 and all(b'cookie:' not in head.lower() for head, _ in seen)
            finally:
                await app.close()
    asyncio.run(run())


def test_network_timeout_and_disconnect():
    async def run():
        closed = asyncio.Event()
        async def handler(reader, writer):
            await read_request(reader)
            await reader.read()
            closed.set()
        async with upstream(handler) as url:
            app = ProxyApplication(config(url, timeout=.08))
            try:
                assert (await request(app))[0]['status'] == 504
                await asyncio.wait_for(closed.wait(), 1)
                closed.clear()
                disconnect = asyncio.Event()
                task = asyncio.create_task(request(app, disconnect=disconnect))
                await asyncio.sleep(.02)
                disconnect.set()
                await asyncio.wait_for(task, 1)
                await asyncio.wait_for(closed.wait(), 1)
            finally:
                await app.close()
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            app = ProxyApplication(config(f'http://127.0.0.1:{unavailable.getsockname()[1]}'))
            try:
                result = await request(app)
                assert result[0]['status'] == 502 and result[1]['body'] == b'Bad Gateway'
            finally:
                await app.close()
    asyncio.run(run())


def test_sse_idle_not_total_and_broken_stream():
    async def run():
        async def handler(reader, writer):
            await read_request(reader)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n')
            for _ in range(6):
                writer.write(b'9\r\ndata: x\n\n\r\n')
                await writer.drain()
                await asyncio.sleep(.025)
            writer.write(b'0\r\n\r\n')
            await writer.drain()
        async with upstream(handler) as url:
            app = ProxyApplication(config(url, timeout=.1))
            try:
                result = await request(app)
                assert result[0]['status'] == 200
                assert b''.join(m.get('body', b'') for m in result) == b'data: x\n\n' * 6
            finally:
                await app.close()
        async def broken(reader, writer):
            await read_request(reader)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx')
            await writer.drain()
        async with upstream(broken) as url:
            app = ProxyApplication(config(url))
            try:
                with pytest.raises(RuntimeError, match='Proxy upstream stream interrupted'):
                    await request(app)
            finally:
                await app.close()
    asyncio.run(run())


def test_proxy_servers_isolates_bad_entry_and_occupied_port(monkeypatch):
    from settings import http_proxy as settings
    async def run():
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0))
                good_port = free.getsockname()[1]
            monkeypatch.setattr(settings, 'application_address', lambda: ('127.0.0.1', 8000))
            monkeypatch.setattr(settings, 'load_values', lambda _: {'proxies': [None, config('http://localhost', port=port), config('http://localhost', port=good_port)]})
            manager = ProxyServers()
            await manager.start()
            try:
                assert len(manager.errors) == 2 and len(manager.servers) == 1
                assert 'Proxy 1' in manager.errors[0] and 'Proxy 2' in manager.errors[1]
            finally:
                await manager.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'disconnect'])
def test_sse_failure_after_response_started(failure):
    async def run():
        async def handler(reader, writer):
            await read_request(reader)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: x\n\n\r\n')
            await writer.drain()
            if failure == 'timeout':
                await asyncio.sleep(1)
        async with upstream(handler) as url:
            app = ProxyApplication(config(url, timeout=.05))
            sent = []
            first = True
            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await asyncio.Event().wait()
            async def send(message):
                sent.append(message)
            try:
                with pytest.raises(RuntimeError, match='Proxy upstream stream interrupted'):
                    await app({'type': 'http', 'method': 'GET', 'raw_path': b'/', 'query_string': b'', 'headers': []}, receive, send)
                assert [m['status'] for m in sent if m['type'] == 'http.response.start'] == [200]
                assert sent[-1]['more_body'] is True
            finally:
                await app.close()
    asyncio.run(run())


@pytest.mark.parametrize('host,client_host,family', [('0.0.0.0', '127.0.0.1', socket.AF_INET), ('::', '::1', socket.AF_INET6)])
def test_wildcard_bind_address(monkeypatch, host, client_host, family):
    from settings import http_proxy as settings

    async def run():
        try:
            with socket.socket(family) as free:
                free.bind((host, 0))
                port = free.getsockname()[1]
        except OSError:
            pytest.skip('IPv6 is unavailable on this host')
        monkeypatch.setattr(settings, 'application_address', lambda: (host, 8000))
        monkeypatch.setattr(settings, 'load_values', lambda _: {'proxies': [config('http://localhost', port=port)]})
        manager = ProxyServers()
        await manager.start()
        try:
            assert not manager.errors
            reader, writer = await asyncio.open_connection(client_host, port)
            writer.write(b'TRACE / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
            await writer.drain()
            assert b'403' in await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            await manager.stop()
    asyncio.run(run())
