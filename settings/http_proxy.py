from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SUPPORTED_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')


def application_address() -> tuple[str, int | None]:
    host = os.environ.get('APP_WEB_HOST', '127.0.0.1')
    port = os.environ.get('APP_WEB_PORT')
    if any('uvicorn' in argument for argument in sys.argv[:2]):
        args = sys.argv[1:]
        for index, argument in enumerate(args):
            for flag in ('host', 'port'):
                value = None
                if argument.startswith(f'--{flag}='):
                    value = argument.split('=', 1)[1]
                elif argument == f'--{flag}' and index + 1 < len(args):
                    value = args[index + 1]
                if value is not None:
                    if flag == 'host' and 'APP_WEB_HOST' not in os.environ:
                        host = value
                    elif flag == 'port' and 'APP_WEB_PORT' not in os.environ:
                        port = value
        if port is None:
            port = '8000'
    try:
        return host, int(port) if port is not None else None
    except ValueError:
        return host, None


def load_values(user_dir: Path) -> dict:
    path = Path(user_dir) / 'http_proxy.json'
    if not path.exists():
        return {'proxies': []}
    with path.open(encoding='utf-8') as file:
        values = json.load(file)
    if not isinstance(values, dict) or not isinstance(values.get('proxies'), list):
        raise ValueError(['HTTP proxy: invalid saved configuration.'])
    return values


def _integer(value, field: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+', value.strip()):
        return int(value)
    raise ValueError(f'{field}: expected an integer.')


def _remote_url(value) -> str:
    if not isinstance(value, str) or not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError('remote URL: expected an HTTP(S) origin.')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or '?' in value or '#' in value
                or '\\' in value or '%' in parsed.netloc
                or parsed.netloc.endswith(':')
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ValueError('remote URL: expected an HTTP(S) origin.') from None
    return value


def validate_values(values, main_port: int | None = None) -> dict:
    if not isinstance(values, dict) or not isinstance(values.get('proxies'), list):
        raise ValueError(['HTTP proxy: proxies must be an array.'])
    proxies = []
    ports = set()
    errors = []
    for index, proxy in enumerate(values['proxies'], 1):
        try:
            if not isinstance(proxy, dict):
                raise ValueError('expected a proxy object.')
            port = _integer(proxy.get('port'), 'port')
            if not 8001 <= port <= 65535:
                raise ValueError('port: must be between 8001 and 65535.')
            if port == main_port:
                raise ValueError('port: already used by the application.')
            if port in ports:
                raise ValueError('port: must be unique.')
            ports.add(port)
            timeout = _integer(proxy.get('timeout', 30), 'timeout')
            if timeout <= 0:
                raise ValueError('timeout: must be positive.')
            remote_url = _remote_url(proxy.get('remote_url'))
            methods = proxy.get('methods', [])
            if not isinstance(methods, list) or any(not isinstance(method, str) or method not in (*SUPPORTED_METHODS, 'ALL') for method in methods):
                raise ValueError('methods: unsupported method.')
            if 'ALL' in methods:
                methods = ['ALL']
            patterns = proxy.get('patterns', [])
            if not isinstance(patterns, list):
                raise ValueError('patterns: expected an array.')
            normalized_patterns = []
            for rule in patterns:
                if (not isinstance(rule, dict) or rule.get('operator') not in ('=', '!')
                        or not isinstance(rule.get('pattern'), str)):
                    raise ValueError('patterns: expected an operator and a pattern.')
                if rule['pattern'] != '':
                    try:
                        re.compile(rule['pattern'])
                    except re.error:
                        raise ValueError('pattern: invalid Python regular expression.') from None
                    normalized_patterns.append({'operator': rule['operator'], 'pattern': rule['pattern']})
            proxies.append({'port': port, 'timeout': timeout, 'remote_url': remote_url,
                            'methods': methods, 'patterns': normalized_patterns})
        except ValueError as error:
            errors.append(f'Proxy {index}: {error}')
    if errors:
        raise ValueError(errors)
    return {'proxies': proxies}


def check_availability(values: dict) -> None:
    errors = []
    for index, proxy in enumerate(values['proxies'], 1):
        try:
            with httpx.Client(verify=False, trust_env=False, follow_redirects=False,
                              timeout=proxy['timeout']) as client:
                with client.stream('GET', proxy['remote_url']):
                    pass
        except (httpx.HTTPError, OSError):
            errors.append(f'Proxy {index}: remote URL is not reachable.')
    if errors:
        raise ValueError(errors)


def save_values(values: dict, user_dir: Path) -> None:
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=user_dir, prefix='.http_proxy.')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(values, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(temporary, user_dir / 'http_proxy.json')
    finally:
        Path(temporary).unlink(missing_ok=True)
