from __future__ import annotations

import re
from urllib.parse import urlsplit


def validate(values):
    errors = []
    host = values.get('remote_host')
    try:
        if not isinstance(host, str) or re.search(r'[\s\\%]', host):
            raise ValueError
        parsed = urlsplit(host)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or '?' in host or '#' in host
                or parsed.port == 0):
            raise ValueError
    except (ValueError, TypeError):
        errors.append('remote_host must be an HTTPS host without credentials, path or query')
    token = values.get('token')
    if not isinstance(token, str) or not token or any(ord(char) < 32 or ord(char) == 127 for char in token):
        errors.append('token is required and must not contain control characters')
    username = values.get('https_username', 'x-access-token')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]+', username):
        errors.append('https_username must be a valid Basic Auth username')
    timeout = values.get('timeout', 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append('timeout must be a positive integer in seconds')
    if values.get('ssh_key'):
        errors.append('SSH transport is not supported in this iteration')
    return errors
