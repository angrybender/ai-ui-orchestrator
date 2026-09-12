from __future__ import annotations

import re
import time
from typing import Any

COMMAND_TIMEOUT = 10


def _split_host(host: str) -> tuple[str, int]:
    port = 22
    if host.startswith('['):
        match = re.fullmatch(r'\[([^\[\]]+)\](?::([0-9]+))?', host)
        if not match:
            raise ValueError
        host, number = match.groups()
        port = int(number) if number else 22
    elif host.count(':') == 1:
        host, number = host.rsplit(':', 1)
        port = int(number)
    if not host or any(c.isspace() or ord(c) < 32 for c in host) or not 1 <= port <= 65535:
        raise ValueError
    return host, port


def validate_connection(values: dict[str, Any]) -> str | None:
    try:
        import paramiko
    except ImportError:
        return 'SSH validation requires the paramiko package'

    client = paramiko.SSHClient()
    channel = None
    try:
        hostname, port = _split_host(str(values.get('host', '')))
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        key = values.get('ssh_key') or None
        client.connect(
            hostname=hostname,
            port=port,
            username=values.get('username'),
            password=None if key else values.get('password') or None,
            key_filename=key,
            allow_agent=False,
            look_for_keys=False,
            timeout=5,
            banner_timeout=5,
            auth_timeout=5,
        )
        deadline = time.monotonic() + COMMAND_TIMEOUT
        _, stdout, _ = client.exec_command(values.get('test_shell') or '', timeout=COMMAND_TIMEOUT)
        channel = stdout.channel
        while True:
            if time.monotonic() >= deadline:
                return 'SSH test command timed out'
            if channel.recv_ready():
                channel.recv(32768)
            if channel.recv_stderr_ready():
                channel.recv_stderr(32768)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                exit_status = channel.recv_exit_status()
                if exit_status != 0:
                    return f'SSH test command failed with exit code {exit_status}'
                return None
            time.sleep(0.01)
    except Exception:
        return 'SSH connection or test command failed'
    finally:
        try:
            if channel is not None:
                channel.close()
        finally:
            client.close()


def validate(values: dict[str, Any]) -> list[str]:
    if not values.get('password') and not values.get('ssh_key'):
        return ['Authentication: password or SSH key is required']
    connection_error = validate_connection(values)
    return [connection_error] if connection_error else []
