"""Identify the machine/PID namespace in which local executor PIDs are valid."""
import json
import os
from pathlib import Path
import socket


def executor_scope():
    machine = socket.gethostname()
    namespace = None
    if os.name != 'nt':
        # Namespace identity matters even when containers share a hostname and
        # machine-id. Boot ID remains in process_token for PID-reuse detection.
        namespace = os.readlink('/proc/self/ns/pid')
        identity = Path('/etc/machine-id')
        if identity.is_file():
            machine += ':' + identity.read_text().strip()
    return json.dumps([os.name, machine, namespace], separators=(',', ':'))
