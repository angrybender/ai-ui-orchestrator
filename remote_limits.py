"""Bounded, cached SSH availability and subscription quota metadata."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shlex
import threading
import time

import paramiko

from settings.config import Config
from settings.validation.remote_server import _split_host

KEYS = ('remote_server.host', 'remote_server.username', 'remote_server.password',
        'remote_server.ssh_key', 'agent.shell')
PROBE = Path(__file__).with_name('remote_limits_probe.py')
_lock = threading.Lock()
_cached = None
_cached_config = None
_expires = 0


def backend(shell):
    """Recognize direct CLI or acp-wrapper CLI argv, never execute agent.shell."""
    try:
        words = shlex.split(shell or '')
    except ValueError:
        return None, None
    # Complex shell programs cannot be safely resolved as executable paths.
    if any(word in (';', '&&', '||', '|') for word in words):
        return None, None
    if not words:
        return None, None
    index = 1 if Path(words[0]).name == 'acp-wrapper' else 0
    if index >= len(words):
        return None, None
    executable = words[index]
    name = Path(executable).name
    return (name, executable) if name in ('codex', 'claude') else (None, None)


def normalize(raw, agent):
    windows = []
    if not isinstance(raw, dict):
        return windows
    for name in (('weekly', '5-hours') if agent == 'claude' else ('weekly',)):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get('used_percent'), window.get('resets_at')
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        try:
            if type(reset) in (int, float) and math.isfinite(reset):
                date = datetime.fromtimestamp(reset, timezone.utc)
            elif isinstance(reset, str):
                date = datetime.fromisoformat(reset.replace('Z', '+00:00'))
                if date.tzinfo is None:
                    continue
            else:
                continue
            reset = date.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        # Expired snapshots must not look like current limits.
        if date.timestamp() <= time.time():
            continue
        windows.append({'name': name, 'used_percent': used, 'resets_at': reset})
    return windows


def probe(config):
    agent, executable = backend(config.get('agent.shell'))
    result = {'reachable': False, 'agent': agent, 'limits': []}
    client = paramiko.SSHClient()
    channel = None
    try:
        host, port = _split_host(config.get('remote_server.host') or '')
        username = config.get('remote_server.username')
        key = config.get('remote_server.ssh_key') or None
        password = config.get('remote_server.password') or None
        if not username or not (key or password):
            return result
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(hostname=host, port=port, username=username, key_filename=key,
                       password=None if key else password, allow_agent=False, look_for_keys=False,
                       timeout=5, auth_timeout=5, banner_timeout=5)
        result['reachable'] = True
        if not agent:
            return result
        channel = client.get_transport().open_session(timeout=5)
        channel.settimeout(5)
        channel.exec_command('python3 - ' + shlex.quote(agent) + ' ' + shlex.quote(executable))
        channel.sendall(PROBE.read_bytes())
        channel.shutdown_write()
        output = bytearray()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if channel.recv_ready():
                output.extend(channel.recv(65536))
                if len(output) > 65536:
                    return result
            if channel.recv_stderr_ready():
                channel.recv_stderr(65536)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                if channel.recv_exit_status() == 0:
                    result['limits'] = normalize(json.loads(output), agent)
                break
            time.sleep(.02)
    except Exception:
        # SSH failure is red; unsupported/missing quota metadata keeps SSH blue.
        pass
    finally:
        if channel is not None:
            channel.close()
        client.close()
    return result


def status():
    global _cached, _cached_config, _expires
    config = Config.snapshot(KEYS)
    with _lock:
        if _cached is None or config != _cached_config or time.monotonic() >= _expires:
            _cached = probe(config)
            _cached_config = config
            _expires = time.monotonic() + 60
        return deepcopy(_cached)
