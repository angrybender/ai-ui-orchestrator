import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from controllers import git_proxy as proxy


@pytest.fixture
def upstream(monkeypatch):
    values = {'remote_host': 'https://github.com', 'token': 'test-secret', 'timeout': 10}
    monkeypatch.setattr(proxy, 'config', lambda: values)
    monkeypatch.setattr(proxy, 'locks', {})
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(proxy.httpx, 'AsyncClient', lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(handler)))
    return values, install


class Chunks(httpx.AsyncByteStream):
    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    async def __aiter__(self):
        yield b'first'
        if self.fail:
            raise httpx.ReadError('test-secret')
        yield b'second'

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('status', [401, 403, 404, 500, 302])
def test_upstream_errors_preserve_status_without_secrets(upstream, status):
    _, install = upstream
    install(lambda request: httpx.Response(status, text='test-secret /private/key',
                                          headers={'location': 'https://other.example'}))
    response = TestClient(main.app).get('/git/org/repo.git/info/refs?service=git-upload-pack', follow_redirects=False)
    assert response.status_code == status
    assert 'test-secret' not in response.text
    assert 'location' not in response.headers


def test_post_stream_auth_headers_cleanup(upstream, caplog):
    _, install = upstream
    chunks = Chunks()

    async def handler(request):
        assert str(request.url) == 'https://github.com/org/repo.git/git-receive-pack'
        assert request.headers['authorization'] == 'Basic ' + base64.b64encode(b'x-access-token:test-secret').decode()
        assert request.headers['git-protocol'] == 'version=2'
        assert await request.aread() == b'payload'
        return httpx.Response(200, stream=chunks, headers={'content-type': 'application/x-git-receive-pack-result'})

    install(handler)
    with caplog.at_level('INFO'):
        response = TestClient(main.app).post('/git/org/repo.git/git-receive-pack', content=b'payload',
                                             headers={'authorization': 'client-secret', 'git-protocol': 'version=2'})
    assert response.content == b'firstsecond'
    assert response.headers['content-type'] == 'application/x-git-receive-pack-result'
    assert chunks.closed
    assert 'test-secret' not in caplog.text
    assert 'bytes_in=7' in caplog.text


@pytest.mark.parametrize('path', [
    '/git/org/repo.git/other?service=git-upload-pack',
    '/git/org/repo.git/info/refs?service=git-upload-pack&service=git-upload-pack',
    '/git/org/repo.git/info/refs?service=git-upload-pack&bad',
    '/git/org/%252e%252e/repo/info/refs?service=git-upload-pack',
    '/git/org//repo/info/refs?service=git-upload-pack',
])
def test_invalid_endpoints(upstream, path):
    assert TestClient(main.app).get(path).status_code == 400


def test_invalid_configuration(upstream):
    values, _ = upstream
    values['remote_host'] = 'https://user:secret@github.com'
    assert TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack').status_code == 503


def test_connection_failure(upstream):
    _, install = upstream

    def handler(request):
        raise httpx.ConnectError('test-secret')
    install(handler)
    response = TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack')
    assert response.status_code == 502
    assert 'test-secret' not in response.text


def test_stream_failure_is_not_success(upstream):
    _, install = upstream
    chunks = Chunks(fail=True)
    install(lambda request: httpx.Response(200, stream=chunks))
    with pytest.raises(RuntimeError, match='Git upstream stream interrupted'):
        TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack')
    assert chunks.closed


def test_deadline(upstream):
    values, install = upstream
    values['timeout'] = 1

    async def handler(request):
        await asyncio.sleep(2)
    install(handler)
    response = TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack')
    assert response.status_code == 504
