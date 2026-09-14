from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import time
from urllib.parse import parse_qs, urlsplit

import httpx
try:
    import paramiko
except ImportError:  # optional until SSH is configured
    paramiko = None
from fastapi import APIRouter, Request
from fastapi.responses import Response

from settings import settings as store
from settings.validation.git_proxy import validate

router = APIRouter()
locks: dict[str, asyncio.Lock] = {}
log = logging.getLogger('git_proxy')


def validate_repo(path: str) -> str:
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', path)
            or any(part in ('.', '..') for part in path.split('/'))):
        raise ValueError('invalid repository path')
    return path


def ssh_target(remote: str, repo: str):
    try:
        if remote.startswith('ssh://'):
            parsed = urlsplit(remote)
            if parsed.password or parsed.query or parsed.fragment:
                raise ValueError
            host, port, base = parsed.hostname, parsed.port or 22, parsed.path
        else:
            address = remote.split('@', 1)[-1]
            host, separator, base = address.partition(':')
            port = 22
            if not separator:
                host, _, base = address.partition('/')
        if not host or any(char.isspace() for char in host):
            raise ValueError
        path = '/'.join(part.strip('/') for part in (base, repo) if part.strip('/'))
        validate_repo(path)
        return host, port, path
    except ValueError as exc:
        raise OSError('Invalid SSH remote') from exc


def config() -> dict:
    section = next((item for item in store.load_sections() if item['key'] == 'git_proxy'), None)
    return store.load_values(section, for_ui=False) if section else {}


class GitResponse(Response):
    def __init__(self, request, values, repo, endpoint, query):
        super().__init__()
        self.request, self.values = request, values
        self.repo, self.endpoint, self.query = repo, endpoint, query

    async def __call__(self, scope, receive, send):
        started = time.monotonic()
        status, incoming, outgoing = 502, 0, 0
        response_started = False
        values = self.values
        timeout = values.get('timeout', 10)
        self.response_started = False
        try:
            async with asyncio.timeout(timeout):
                async with locks.setdefault(self.repo, asyncio.Lock()):
                    if values.get('ssh_key'):
                        status, incoming, outgoing, response_started = await self._ssh(scope, receive, send)
                    else:
                        status, incoming, outgoing, response_started = await self._https(scope, receive, send)
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            if paramiko is not None and isinstance(exc, paramiko.SSHException):
                pass
            if response_started or self.response_started:
                raise RuntimeError('Git upstream stream interrupted') from exc
            status = 504 if isinstance(exc, TimeoutError) else 502
            await Response('Git upstream unavailable', status, media_type='text/plain')(scope, receive, send)
        finally:
            log.info('git_proxy repo=%s operation=%s status=%s duration=%.3f bytes_in=%s bytes_out=%s',
                     self.repo, self.endpoint, status, time.monotonic() - started, incoming, outgoing)

    async def _https(self, scope, receive, send):
        values = self.values
        timeout = values.get('timeout', 10)
        headers = {key: value for key, value in self.request.headers.items()
                   if key.lower() in ('content-type', 'content-encoding', 'git-protocol')}
        auth = f"{values.get('https_username', 'x-access-token')}:{values['token']}".encode()
        headers['authorization'] = 'Basic ' + base64.b64encode(auth).decode()
        headers['accept-encoding'] = 'identity'
        incoming = outgoing = 0
        response_started = False

        async def body():
            nonlocal incoming
            async for chunk in self.request.stream():
                incoming += len(chunk)
                yield chunk

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            url = f"{values['remote_host'].rstrip('/')}/{self.repo}/{self.endpoint}"
            async with client.stream(self.request.method, url, params=self.query, headers=headers,
                                     content=body() if self.request.method == 'POST' else None) as upstream:
                status = upstream.status_code
                if not 200 <= status < 300:
                    await Response('Git upstream rejected the request', status,
                                   media_type='text/plain')(scope, receive, send)
                    return status, incoming, outgoing, False
                allowed = {'content-type', 'content-encoding', 'cache-control', 'pragma', 'expires'}
                output_headers = [(k, v) for k, v in upstream.headers.raw if k.decode().lower() in allowed]
                await send({'type': 'http.response.start', 'status': status, 'headers': output_headers})
                response_started = True
                self.response_started = True
                async for chunk in upstream.aiter_raw():
                    outgoing += len(chunk)
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        return status, incoming, outgoing, response_started

    async def _ssh(self, scope, receive, send):
        if paramiko is None:
            raise OSError('SSH transport unavailable')
        host, port, base_repo = ssh_target(self.values['remote_host'], self.repo)
        service = self.query.get('service', [self.endpoint])[0] if self.endpoint == 'info/refs' else self.endpoint
        command = f"{service} {shlex.quote(base_repo)}"
        client = paramiko.SSHClient()
        channel = None
        pump_task = None
        incoming = outgoing = 0
        timeout = self.values.get('timeout', 10)
        try:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            await asyncio.to_thread(client.connect, hostname=host, port=port,
                                    username=self.values.get('ssh_username', 'git'),
                                    key_filename=self.values['ssh_key'], allow_agent=False,
                                    look_for_keys=False, timeout=timeout,
                                    banner_timeout=timeout, auth_timeout=timeout)
            channel = await asyncio.to_thread(client.get_transport().open_session, timeout=timeout)
            channel.settimeout(timeout)
            await asyncio.to_thread(channel.exec_command, command)

            async def read_chunk(size):
                while True:
                    if channel.recv_stderr_ready():
                        await asyncio.to_thread(channel.recv_stderr, 65536)
                    if channel.recv_ready():
                        return await asyncio.to_thread(channel.recv, size)
                    if channel.eof_received or channel.closed:
                        return b''
                    if pump_task is not None and pump_task.done():
                        await pump_task
                    await asyncio.sleep(0.001)

            async def read_exact(size):
                data = bytearray()
                while len(data) < size:
                    chunk = await read_chunk(size - len(data))
                    if not chunk:
                        raise OSError('Incomplete Git advertisement')
                    data.extend(chunk)
                return bytes(data)

            async def advertisement():
                while True:
                    header = await read_exact(4)
                    if not re.fullmatch(b'[0-9a-fA-F]{4}', header):
                        raise OSError('Invalid Git packet header')
                    size = int(header, 16)
                    if size == 0:
                        yield header
                        return
                    if not 4 <= size <= 65520:
                        raise OSError('Invalid Git packet size')
                    payload = await read_exact(size - 4)
                    if payload.startswith(b'ERR '):
                        raise OSError('Git upstream rejected the request')
                    yield header + payload

            async def start_response(suffix):
                content_type = f'application/x-{service}-{suffix}'
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(b'content-type', content_type.encode()),
                                        (b'cache-control', b'no-cache')]})
                self.response_started = True

            async def write(data):
                nonlocal outgoing
                outgoing += len(data)
                await send({'type': 'http.response.body', 'body': data, 'more_body': True})

            if self.endpoint == 'info/refs':
                async for packet in advertisement():
                    if not self.response_started:
                        await start_response('advertisement')
                        body = f'# service={service}\n'.encode()
                        await write(f'{len(body) + 4:04x}'.encode() + body + b'0000')
                    await write(packet)
            else:
                # SSH git-upload-pack advertises refs before reading the POST body.
                # Smart HTTP has already received that advertisement in info/refs.
                async for _ in advertisement():
                    pass

                async def pump():
                    nonlocal incoming
                    async for chunk in self.request.stream():
                        if chunk:
                            incoming += len(chunk)
                            await asyncio.to_thread(channel.sendall, chunk)
                    await asyncio.to_thread(channel.shutdown_write)

                pump_task = asyncio.create_task(pump())
                while data := await read_chunk(65536):
                    if not self.response_started:
                        await start_response('result')
                    await write(data)
                await pump_task
                if await asyncio.to_thread(channel.recv_exit_status) != 0:
                    raise OSError('Git upstream command failed')
                if not self.response_started:
                    await start_response('result')
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return 200, incoming, outgoing, True
        except paramiko.SSHException:
            raise OSError('SSH upstream unavailable') from None
        finally:
            if channel is not None:
                channel.close()
            client.close()
            if pump_task is not None:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)


@router.api_route('/git/{repo_path:path}', methods=['GET', 'POST'])
async def git_proxy(request: Request, repo_path: str):
    try:
        if request.method == 'GET':
            if not repo_path.endswith('/info/refs'):
                raise ValueError
            repo, endpoint = repo_path[:-10], 'info/refs'
            query = parse_qs(request.url.query, keep_blank_values=True, strict_parsing=True)
            if set(query) != {'service'} or query['service'] not in (['git-upload-pack'], ['git-receive-pack']):
                raise ValueError
        else:
            repo, endpoint = repo_path.rsplit('/', 1)
            query = {}
            if request.url.query or endpoint not in ('git-upload-pack', 'git-receive-pack'):
                raise ValueError
        validate_repo(repo)
    except ValueError:
        return Response('not allowed', 400, media_type='text/plain')
    try:
        values = config()
        if validate(values):
            return Response('Git proxy is not configured', 503, media_type='text/plain')
    except (ValueError, OSError, TypeError):
        return Response('Git proxy is not configured', 503, media_type='text/plain')
    return GitResponse(request, values, repo, endpoint, query)
