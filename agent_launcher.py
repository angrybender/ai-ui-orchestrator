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
            # Continue only the WAIT transition owned by the finished run.
            # The new executor still chooses/reserves its task in claim().
            waiting = connection.execute(
                "SELECT 1 FROM agent_runs r JOIN board_tasks t ON t.id = r.task_pk "
                "WHERE r.id = ? AND r.state = 'FAILED' AND t.status = 'WAIT' AND t.active_run_id = r.id",
                (after_wait,),
            ).fetchone()
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
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name('agent_worker.py')),
                 '--continue-after-wait',
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
