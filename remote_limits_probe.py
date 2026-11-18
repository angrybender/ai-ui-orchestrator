"""Executed over SSH stdin; credentials and provider responses stay remote."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def codex_limits(executable):
    process = subprocess.Popen([executable, 'app-server'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, bufsize=0)
    deadline = time.monotonic() + 15
    buffer = b''

    def request(number, method, params):
        nonlocal buffer
        process.stdin.write((json.dumps({'id': number, 'method': method, 'params': params}) + '\n').encode())
        while time.monotonic() < deadline:
            if b'\n' not in buffer:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 1048576:
                    raise ValueError('Response too large')
                continue
            line, buffer = buffer.split(b'\n', 1)
            message = json.loads(line)
            if message.get('id') == number:
                if 'error' in message:
                    return {}
                return message.get('result') or {}
        raise TimeoutError

    try:
        request(1, 'initialize', {'clientInfo': {'name': 'orchestrator-limits', 'version': '1.0'}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        result = request(2, 'account/rateLimits/read', {})
        limits = result.get('rateLimits') or {}
        # Only the weekly subscription window is displayed for Codex.
        for key in ('secondary', 'primary'):
            window = limits.get(key) or {}
            if window.get('windowDurationMins') == 10080:
                return {'weekly': {'used_percent': window.get('usedPercent'), 'resets_at': window.get('resetsAt')}}
        return {}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
        process.stdout.close()


def kimi_limits(executable):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [executable, 'web', '--no-open', '--host', '127.0.0.1', '--port', str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.monotonic() + 15
    token = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Kimi server exited')
            if token is None:
                try:
                    home = Path(os.environ.get('KIMI_CODE_HOME') or Path.home() / '.kimi-code')
                    token = (home / 'server.token').read_text().strip()
                except OSError:
                    pass
            if token:
                try:
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{port}/api/v1/oauth/usage',
                        headers={'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(request, timeout=min(3, max(.01, deadline - time.monotonic()))) as response:
                        content = response.read(65537)
                    if len(content) > 65536:
                        raise ValueError('Response too large')
                    payload = json.loads(content)
                    data = payload.get('data') or {}
                    if payload.get('code') != 0 or data.get('kind') != 'ok':
                        return {}
                    usages = (data.get('quota') or {}).get('usages') or {}
                    result = {}
                    for source, name in [('limit7d', 'weekly'), ('limit5h', '5-hours'),
                                         ('monthTotal', 'month-total'), ('monthCode', 'month-code')]:
                        window = usages.get(source) or {}
                        ratio = window.get('usedRatio')
                        if type(ratio) in (int, float):
                            result[name] = {'used_percent': round(ratio * 100, 10), 'resets_at': window.get('resetAt')}
                    return result
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            time.sleep(.1)
        raise TimeoutError
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)


def claude_limits():
    if os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'):
        return {}
    if os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com').rstrip('/') != 'https://api.anthropic.com':
        return {}
    directory = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    with (directory / '.credentials.json').open() as stream:
        token = json.load(stream).get('claudeAiOauth', {}).get('accessToken')
    if not isinstance(token, str) or not token:
        return {}
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': 'orchestrator-limits/1.0'})
    with urllib.request.urlopen(request, timeout=12) as response:
        result = json.loads(response.read(65536))
    return {name: {'used_percent': (result.get(key) or {}).get('utilization'),
                   'resets_at': (result.get(key) or {}).get('resets_at')}
            for name, key in [('weekly', 'seven_day'), ('5-hours', 'five_hour')]}


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'codex':
            result = codex_limits(sys.argv[2])
        elif sys.argv[1] == 'kimi':
            result = kimi_limits(sys.argv[2])
        else:
            result = claude_limits()
    except Exception:
        result = {}
    print(json.dumps(result), flush=True)
