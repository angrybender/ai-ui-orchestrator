from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from uuid import uuid4

import board_store

ACTIVE = "state IN ('PREPARING', 'RUNNING')"


def init_database() -> None:
    with board_store.connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                task_pk INTEGER REFERENCES board_tasks(id) ON DELETE SET NULL,
                state TEXT NOT NULL CHECK(state IN ('PREPARING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
                pid INTEGER NOT NULL,
                process_token TEXT NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                timeout REAL NOT NULL,
                session_id TEXT,
                stop_reason TEXT,
                error TEXT,
                log TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS agent_single_active
                ON agent_runs((1)) WHERE state IN ('PREPARING', 'RUNNING');
            CREATE UNIQUE INDEX IF NOT EXISTS agent_task_active
                ON agent_runs(task_pk) WHERE state IN ('PREPARING', 'RUNNING');
        """)

        columns = {row['name'] for row in connection.execute('PRAGMA table_info(agent_runs)')}
        migrate_ownership = 'phase' not in columns
        for name, definition in {'phase': "TEXT NOT NULL DEFAULT 'In progress'",
                                 'init_pid': 'INTEGER', 'init_result': 'TEXT',
                                 'init_uncertain': 'INTEGER NOT NULL DEFAULT 0'}.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE agent_runs ADD COLUMN {name} {definition}')
        if migrate_ownership:
            connection.execute("UPDATE board_tasks SET active_run_id = (SELECT id FROM agent_runs WHERE task_pk = board_tasks.id AND state IN ('PREPARING', 'RUNNING')) WHERE status = 'IN PROGRESS'")


def process_identity(pid: int) -> str | None:
    if os.name == 'nt':
        from windows_process import process_identity as windows_identity

        return windows_identity(pid)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{stat[19]}'
    except FileNotFoundError:
        return None


def active_run() -> dict | None:
    with board_store.connect() as connection:
        row = connection.execute(f'SELECT * FROM agent_runs WHERE {ACTIVE}').fetchone()
        return dict(row) if row else None


def finish(run_id: str, error: str | None = None, stop_reason: str | None = None, *, init_error=False, uncertain=False) -> None:
    with board_store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(f'SELECT * FROM agent_runs WHERE id = ? AND {ACTIVE}', (run_id,)).fetchone()
        if row is None:
            return
        if row['phase'] == 'Init' and row['init_result'] != 'SUCCEEDED':
            init_error = True
            uncertain = uncertain or bool(row['init_uncertain'])
        if uncertain:
            connection.execute('UPDATE board_tasks SET init_uncertain = 1 WHERE id = ?', (row['task_pk'],))
        if init_error:
            connection.execute("UPDATE agent_runs SET init_result = 'FAILED', init_uncertain = ? WHERE id = ?", (uncertain, run_id))
            error = error or 'Init script error: executor interrupted'
            if not error.startswith('Init script error'):
                error = 'Init script error: ' + error
            if uncertain:
                error += '\nRemote stop unconfirmed; retry blocked pending verification.'
        connection.execute(
            'UPDATE agent_runs SET state = ?, finished_at = ?, error = ?, stop_reason = ? WHERE id = ?',
            ('FAILED' if error else 'SUCCEEDED', time.time(), error, stop_reason, run_id),
        )
        connection.execute(
            'UPDATE board_tasks SET status = ?, is_error = ? WHERE id = ? AND status = \'IN PROGRESS\' AND active_run_id = ?',
            ('WAIT' if error else 'REVIEW', bool(error), row['task_pk'], run_id),
        )
        if error and row['task_pk'] is not None:
            updated_at = "strftime('%Y-%m-%d %H:%M', 'now', 'localtime')"
            connection.execute(
                f"INSERT INTO task_chat_messages(task_pk, role, text, run_id, updated_at) VALUES (?, ?, ?, ?, {updated_at}) "
                "ON CONFLICT(task_pk, run_id, role) DO UPDATE SET "
                "text = task_chat_messages.text || char(10) || char(10) || excluded.text, "
                "updated_at = excluded.updated_at",
                (row['task_pk'], 'system' if init_error else 'agent', error, run_id),
            )


def recover() -> None:
    row = active_run()
    if row is None:
        return
    try:
        identity = process_identity(row['pid'])
        expired = time.time() - (row['started_at'] or row['created_at']) > row['timeout'] + (10 if row['phase'] == 'Init' else 0)
        if identity == row['process_token']:
            if not expired:
                return
            if row['pid'] == os.getpid():
                return
            if os.name == 'nt':
                from windows_process import terminate_process

                current = active_run()
                if current is None or current['id'] != row['id']:
                    return
                if time.time() - (current['started_at'] or current['created_at']) <= current['timeout']:
                    return
                if terminate_process(row['pid'], row['process_token']):
                    finish(row['id'], 'Agent executor expired.', uncertain=row['phase'] == 'Init')
                return
            # pidfd prevents signalling an unrelated process after PID reuse.
            fd = os.pidfd_open(row['pid'])
            try:
                current = active_run()
                if current is None or current['id'] != row['id']:
                    return
                if time.time() - (current['started_at'] or current['created_at']) <= current['timeout']:
                    return
                if process_identity(row['pid']) != row['process_token']:
                    return
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while process_identity(row['pid']) == identity and time.monotonic() < deadline:
                    time.sleep(0.02)
                if process_identity(row['pid']) == identity:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while process_identity(row['pid']) == identity and time.monotonic() < deadline:
                        time.sleep(0.02)
                if process_identity(row['pid']) == identity:
                    return
            finally:
                os.close(fd)
        finish(row['id'], 'Agent executor expired.' if expired else 'Agent executor terminated.', uncertain=row['phase'] == 'Init')
    except ProcessLookupError:
        finish(row['id'], 'Agent executor terminated.', uncertain=row['phase'] == 'Init')
    except (OSError, AttributeError):
        # An unverifiable owner must not be replaced by another session.
        return


def claim(timeout: float) -> dict | None:
    recover()
    with board_store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute(f'SELECT 1 FROM agent_runs WHERE {ACTIVE}').fetchone():
            return None
        task = connection.execute("SELECT * FROM board_tasks WHERE status = 'OPEN' AND init_uncertain = 0 ORDER BY sort_order, id LIMIT 1").fetchone()
        if task is None:
            return None
        run_id = uuid4().hex
        token = process_identity(os.getpid())
        if token is None:
            raise RuntimeError('Cannot identify agent executor.')
        connection.execute(
            "INSERT INTO agent_runs(id, task_pk, state, pid, process_token, created_at, timeout) VALUES (?, ?, 'PREPARING', ?, ?, ?, ?)",
            (run_id, task['id'], os.getpid(), token, time.time(), timeout),
        )
        cursor = connection.execute("UPDATE board_tasks SET status = 'IN PROGRESS', is_error = 0, phase = 'In progress', active_run_id = ? WHERE id = ? AND status = 'OPEN'", (run_id, task['id']))
        if cursor.rowcount != 1:
            raise RuntimeError('Task claim failed.')
        comment = connection.execute(
            "SELECT text FROM task_chat_messages WHERE task_pk = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (task['id'],),
        ).fetchone()
        previous = connection.execute(
            "SELECT session_id FROM agent_runs WHERE task_pk = ? AND session_id IS NOT NULL AND session_id != '' ORDER BY created_at DESC LIMIT 1",
            (task['id'],),
        ).fetchone()
        return {'id': run_id, 'task_id': task['task_id'], 'comment': comment['text'] if comment else None,
                'session_id': previous['session_id'] if previous else None,
                'remote_cwd': task['remote_cwd'], 'remote_identity': task['remote_identity'],
                'context_ready': bool(task['context_ready']), 'init_succeeded': bool(task['init_succeeded'])}


def message(run_id: str, text: str) -> None:
    """Replace this run's already sanitized agent response, never its diagnostics."""
    with board_store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(f'SELECT task_pk FROM agent_runs WHERE id = ? AND {ACTIVE} AND EXISTS (SELECT 1 FROM board_tasks WHERE id = task_pk AND active_run_id = agent_runs.id)', (run_id,)).fetchone()
        if row is None or row['task_pk'] is None:
            return
        updated_at = "strftime('%Y-%m-%d %H:%M', 'now', 'localtime')"
        if isinstance(text, str) and text.strip():
            connection.execute(
                f"INSERT INTO task_chat_messages(task_pk, role, text, run_id, updated_at) VALUES (?, 'agent', ?, ?, {updated_at}) "
                "ON CONFLICT(task_pk, run_id, role) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
                (row['task_pk'], text, run_id),
            )
        else:
            connection.execute(
                f"UPDATE task_chat_messages SET updated_at = {updated_at} WHERE task_pk = ? AND role = 'agent' AND run_id = ?",
                (row['task_pk'], run_id),
            )


def started(run_id: str) -> None:
    with board_store.connect() as connection:
        connection.execute(f"UPDATE agent_runs SET state = 'RUNNING', started_at = ? WHERE id = ? AND {ACTIVE}", (time.time(), run_id))


def session(run_id: str, session_id: str) -> None:
    with board_store.connect() as connection:
        connection.execute(f'UPDATE agent_runs SET session_id = ? WHERE id = ? AND {ACTIVE}', (session_id, run_id))


def append_log(run_id: str, text: str) -> None:
    with board_store.connect() as connection:
        connection.execute(f'UPDATE agent_runs SET log = log || ? WHERE id = ? AND {ACTIVE}', (text, run_id))
