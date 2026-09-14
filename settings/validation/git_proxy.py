from __future__ import annotations

import re
from pathlib import Path


def validate(values):
    errors = []
    host = values.get('remote_host')
    key = values.get('ssh_key')
    token = values.get('token')
    has_key = isinstance(key, str) and bool(key.strip())
    has_token = isinstance(token, str) and bool(token.strip())

    if not isinstance(host, str) or not host.strip():
        errors.append('remote_host is required')
    if has_key and has_token:
        errors.append('token and ssh_key cannot be used together')
    if has_key:
        if not Path(key).is_file():
            errors.append('ssh_key must point to an existing file')
        username = values.get('ssh_username')
        if not isinstance(username, str) or not username.strip() or not re.fullmatch(r'[A-Za-z0-9_.@-]+', username):
            errors.append('ssh_username is required and must be valid')
    elif has_token:
        username = values.get('https_username')
        if not isinstance(username, str) or not username.strip() or not re.fullmatch(r'[A-Za-z0-9_.@-]+', username):
            errors.append('https_username is required and must be valid')
    else:
        errors.append('token or ssh_key is required')

    timeout = values.get('timeout', 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append('timeout must be a positive integer in seconds')
    return errors
