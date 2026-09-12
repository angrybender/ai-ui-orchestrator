from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from urllib.parse import parse_qs

import httpx
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
        headers = {key: value for key, value in self.request.headers.items()
                   if key.lower() in ('content-type', 'content-encoding', 'git-protocol')}
        auth = f"{values.get('https_username', 'x-access-token')}:{values['token']}".encode()
        headers['authorization'] = 'Basic ' + base64.b64encode(auth).decode()
        headers['accept-encoding'] = 'identity'

        async def body():
            nonlocal incoming
            async for chunk in self.request.stream():
                incoming += len(chunk)
                yield chunk

        try:
            async with asyncio.timeout(timeout):
                async with locks.setdefault(self.repo, asyncio.Lock()):
                    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                        url = f"{values['remote_host'].rstrip('/')}/{self.repo}/{self.endpoint}"
                        async with client.stream(self.request.method, url, params=self.query, headers=headers,
                                                 content=body() if self.request.method == 'POST' else None) as upstream:
                            status = upstream.status_code
                            # Error bodies can echo credentials or internal paths; never relay them.
                            if not 200 <= status < 300:
                                await Response('Git upstream rejected the request', status,
                                               media_type='text/plain')(scope, receive, send)
                                return
                            allowed = {'content-type', 'content-encoding', 'cache-control', 'pragma', 'expires'}
                            output_headers = [(k, v) for k, v in upstream.headers.raw
                                              if k.decode().lower() in allowed]
                            await send({'type': 'http.response.start', 'status': status, 'headers': output_headers})
                            response_started = True
                            async for chunk in upstream.aiter_raw():
                                outgoing += len(chunk)
                                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        except (httpx.HTTPError, TimeoutError):
            status = 504 if time.monotonic() - started >= timeout else 502
            if response_started:
                # A truncated Git stream must fail, not become a successful empty response.
                raise RuntimeError('Git upstream stream interrupted') from None
            await Response('Git upstream unavailable', status, media_type='text/plain')(scope, receive, send)
        finally:
            log.info('git_proxy repo=%s operation=%s status=%s duration=%.3f bytes_in=%s bytes_out=%s',
                     self.repo, self.endpoint, status, time.monotonic() - started, incoming, outgoing)


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
