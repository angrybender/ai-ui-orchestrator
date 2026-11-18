"""Executed over SSH stdin; credentials and provider responses stay remote."""
import json
import os
from pathlib import Path
import select
import signal
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
        result = codex_limits(sys.argv[2]) if sys.argv[1] == 'codex' else claude_limits()
    except Exception:
        result = {}
    print(json.dumps(result), flush=True)
