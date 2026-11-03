from __future__ import annotations

from contextlib import closing
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


def launch(*, after_wait: str | None = None) -> int | None:
    with closing(board_store.connect()) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        if after_wait is not None:
            # This check avoids pointless processes; claim() repeats the fence
            # atomically after startup, when another worker may have run already.
            waiting = agent_store.wait_continuation_ready(connection, after_wait)
            blocked = connection.execute('SELECT 1 FROM agent_runs WHERE agent_uncertain = 1').fetchone()
            available = connection.execute(
                "SELECT 1 FROM board_tasks WHERE status = 'OPEN' AND init_uncertain = 0 AND agent_uncertain = 0 LIMIT 1"
            ).fetchone()
            if not waiting or blocked or not available:
                return None
        active = connection.execute(f'SELECT * FROM agent_runs WHERE {agent_store.ACTIVE}').fetchone()
        if active:
            if active['executor_scope'] is not None and active['executor_scope'] != agent_store.executor_scope():
                return None
            try:
                identity = agent_store.process_identity(active['pid'])
            except OSError:
                return None
            if identity == active['process_token'] and time.time() - (active['started_at'] or active['created_at']) <= active['timeout']:
                return None
        command = [sys.executable, str(Path(__file__).with_name('agent_worker.py')),
                   '--continue-after-wait',
                   '--database', str(board_store.DATABASE_PATH.resolve()),
                   '--files', str(board_store.FILES_DIR.resolve())]
        if after_wait is not None:
            command.extend(['--after-wait', after_wait])
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **process_options(),
            )
        except OSError:
            logger.error('Unable to create agent executor process.')
            raise RuntimeError('Unable to create agent executor process.') from None
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid
