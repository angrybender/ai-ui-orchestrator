from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import agent_store
import board_store
from agent_consumer import process_options

logger = logging.getLogger(__name__)


def launch() -> int | None:
    with board_store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        active = connection.execute(f'SELECT * FROM agent_runs WHERE {agent_store.ACTIVE}').fetchone()
        if active:
            try:
                identity = agent_store.process_identity(active['pid'])
            except OSError:
                return None
            if identity == active['process_token'] and time.time() - (active['started_at'] or active['created_at']) <= active['timeout']:
                return None
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name('agent_worker.py')),
                 '--database', str(board_store.DATABASE_PATH.resolve()),
                 '--files', str(board_store.FILES_DIR.resolve())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **process_options(),
            )
        except OSError:
            logger.error('Unable to create agent executor process.')
            raise RuntimeError('Unable to create agent executor process.') from None
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid
