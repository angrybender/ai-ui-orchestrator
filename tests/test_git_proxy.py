import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from controllers import git_proxy as proxy


def test_ssh_target_parses_scp_remote():
    assert proxy.ssh_target('git@github.com:angrybender', 'test-09-13.git') == (
        'github.com', 22, 'angrybender/test-09-13.git')


def test_ssh_target_parses_url_remote():
    assert proxy.ssh_target('ssh://git.example:2222/team', 'repo.git') == (
        'git.example', 2222, 'team/repo.git')


def test_ssh_target_rejects_traversal():
    with pytest.raises(OSError):
        proxy.ssh_target('git@example.com:team', '../repo.git')


def test_info_refs_content_type():
    service = 'git-upload-pack'
    assert ('application/x-%s-advertisement' % service) == (
        'application/x-git-upload-pack-advertisement')


def test_info_refs_prefix_uses_pkt_line_framing():
    body = b'# service=git-upload-pack\n'
    prefix = f'{len(body) + 4:04x}'.encode() + body + b'0000'
    assert prefix == b'001e# service=git-upload-pack\n0000'


def test_post_ssh_advertisement_is_consumed_before_result():
    advertisement = b'003fref advertisement\x00capabilities\n0000'
    assert advertisement.endswith(b'0000')
    assert not advertisement.startswith(b'0000')


@pytest.fixture
def upstream(monkeypatch):
    values = {'remote_host': 'https://github.com', 'token': 'test-secret',
              'https_username': 'x-access-token', 'timeout': 10}
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
    values['remote_host'] = ''
    assert TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack').status_code == 503


def test_host_syntax_is_not_validated(upstream):
    values, install = upstream
    values['remote_host'] = 'https://github.com/upstream path'
    install(lambda request: httpx.Response(401, text='rejected'))
    assert TestClient(main.app).get('/git/org/repo/info/refs?service=git-upload-pack').status_code == 401


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
