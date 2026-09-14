from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit

import httpcore
import uvicorn

from settings import http_proxy as proxy_settings
from settings import settings as settings_store

logger = logging.getLogger(__name__)
HOP_HEADERS = {
    b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
    b'te', b'trailer', b'transfer-encoding', b'upgrade', b'proxy-connection',
}


def end_to_end_headers(headers):
    excluded = set(HOP_HEADERS)
    for name, value in headers:
        if name.lower() == b'connection':
            excluded.update(token.strip().lower() for token in value.split(b','))
    return [(name.lower(), value) for name, value in headers if name.lower() not in excluded]


class ProxyApplication:
    def __init__(self, config):
        self.config = config
        self.remote = urlsplit(config['remote_url'])
        self.update_policy(config)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.pool = httpcore.AsyncConnectionPool(ssl_context=context)

    def update_policy(self, config):
        methods = frozenset(config['methods'])
        rules = tuple((rule['operator'], re.compile(rule['pattern']))
                      for rule in config['patterns'] if rule['pattern'] != '')
        # Requests capture both restrictions from one immutable snapshot.
        self.policy = (methods, rules)

    async def close(self):
        await self.pool.aclose()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        method = scope['method']
        target = scope['raw_path']
        if scope['query_string']:
            target += b'?' + scope['query_string']
        uri = target.decode('latin-1')
        methods, rules = self.policy
        allowed = (method in proxy_settings.SUPPORTED_METHODS
                   and (not methods or 'ALL' in methods or method in methods)
                   and all(bool(pattern.match(uri)) == (operator == '=')
                           for operator, pattern in rules))
        if not allowed:
            await self.error(send, method, 403, 'Access denied by proxy server')
            return
        incoming = asyncio.Queue(maxsize=1)

        async def body():
            while True:
                message = await incoming.get()
                if message.get('body'):
                    yield message['body']
                if not message.get('more_body', False):
                    break

        async def disconnected():
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                await incoming.put(message)

        async def forward():
            started = False
            try:
                headers = [(name, value) for name, value in end_to_end_headers(scope['headers'])
                           if name not in (b'host', b'content-length')]
                headers.append((b'host', self.remote.netloc.encode('idna')))
                url = httpcore.URL(scheme=self.remote.scheme,
                                   host=self.remote.hostname.encode('idna'),
                                   port=self.remote.port, target=target)
                timeout = self.config['timeout']
                async with self.pool.stream(
                    method, url, headers=headers, content=body(),
                    extensions={'timeout': dict.fromkeys(('connect', 'read', 'write', 'pool'), timeout)},
                ) as response:
                    if response.status == 101:
                        await self.error(send, method, 502, 'Bad Gateway')
                        return
                    await send({'type': 'http.response.start', 'status': response.status,
                                'headers': end_to_end_headers(response.headers)})
                    started = True
                    async for chunk in response.aiter_stream():
                        if method != 'HEAD':
                            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as error:
                if started:
                    raise RuntimeError('Proxy upstream stream interrupted') from None
                status, text = ((504, 'Gateway Timeout') if isinstance(error, httpcore.TimeoutException)
                                else (502, 'Bad Gateway'))
                await self.error(send, method, status, text)

        transfer = asyncio.create_task(forward())
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((transfer, watcher), return_when=asyncio.FIRST_COMPLETED)
            if transfer in done:
                await transfer
        finally:
            for task in (transfer, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(transfer, watcher, return_exceptions=True)

    @staticmethod
    async def error(send, method, status, text):
        body = text.encode('utf-8')
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'text/plain; charset=utf-8'),
                                (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': b'' if method == 'HEAD' else body})


class ProxyServers:
    def __init__(self):
        self.errors = []
        self.servers = []
        self.save_lock = threading.Lock()

    def apply_policies(self, values):
        saved = {proxy['port']: proxy for proxy in values['proxies']}
        running = {application.config['port']: application for _, _, application, _ in self.servers}
        restart_required = saved.keys() != running.keys()
        for port, application in running.items():
            if port not in saved:
                continue
            proxy = saved[port]
            application.update_policy(proxy)
            if any(proxy[field] != application.config[field] for field in ('remote_url', 'timeout')):
                restart_required = True
        return restart_required

    async def start(self):
        host, main_port = proxy_settings.application_address()
        try:
            values = proxy_settings.load_values(settings_store.USER_SETTINGS_DIR)
        except (ValueError, OSError):
            self.errors.append('HTTP proxy: could not read saved configuration.')
            return
        for index, raw in enumerate(values['proxies'], 1):
            listener = None
            application = None
            try:
                config = proxy_settings.validate_values({'proxies': [raw]}, main_port=main_port)['proxies'][0]
                family = socket.AF_INET6 if ':' in host else socket.AF_INET
                listener = socket.socket(family, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((host, config['port']))
                listener.listen(128)
                listener.setblocking(False)
                application = ProxyApplication(config)
                server_config = uvicorn.Config(application, host=host, port=config['port'],
                    lifespan='off', ws='none', access_log=False, log_config=None,
                    server_header=False, date_header=False, timeout_graceful_shutdown=1)
                server_config.load()
                server = uvicorn.Server(server_config)
                server.lifespan = server_config.lifespan_class(server_config)
                await server.startup(sockets=[listener])
                task = asyncio.create_task(server.main_loop())
                self.servers.append((server, task, application, listener))
            except (ValueError, OSError):
                if listener is not None:
                    listener.close()
                if application is not None:
                    await application.close()
                port = raw.get('port', '?') if isinstance(raw, dict) else '?'
                # Never expose upstream addresses or transport diagnostics in the UI.
                label = port if type(port) is int else '?'
                message = f'Proxy {index} (port {label}): could not start. Check configuration and port availability.'
                self.errors.append(message)
                logger.error(message)

    async def stop(self):
        for server, _, _, _ in self.servers:
            server.should_exit = True
        for server, task, application, listener in self.servers:
            try:
                await task
                await server.shutdown(sockets=[listener])
                await asyncio.gather(*tuple(server.server_state.tasks), return_exceptions=True)
            finally:
                await application.close()
                listener.close()
        self.servers.clear()
