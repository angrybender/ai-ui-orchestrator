"""Reset ACP conversation ownership without deleting run history or task files."""
from contextlib import closing
from datetime import datetime

import board_store


RESET_MESSAGE = 'Agent context cleared. The next run will start a new session.'
RESET_STATUSES = ('BACKLOG', 'WAIT', 'REVIEW')


def _idle(connection, task):
    return (task['status'] in RESET_STATUSES
            and not task['init_uncertain'] and not task['agent_uncertain']
            and not connection.execute(
                "SELECT 1 FROM agent_runs WHERE task_pk = ? AND "
                "(state IN ('PREPARING', 'RUNNING') OR init_uncertain = 1 OR agent_uncertain = 1)",
                (task['id'],)).fetchone())


def _session_exists(connection, task):
    return connection.execute(
        "SELECT 1 FROM agent_runs WHERE task_pk = ? AND rowid > ? "
        "AND session_id IS NOT NULL AND session_id != '' LIMIT 1",
        (task['id'], task['acp_reset_after_run'])).fetchone() is not None


def can_clear_context(connection, task):
    return bool(_idle(connection, task) and _session_exists(connection, task))


def clear_context(task_id):
    with closing(board_store.connect()) as connection, connection:
        # The same write lock as claim(): either reset wins or a running task rejects it.
        connection.execute('BEGIN IMMEDIATE')
        task = connection.execute('SELECT * FROM board_tasks WHERE task_id = ?', (task_id,)).fetchone()
        if task is None:
            raise board_store.TaskNotFoundError('Task not found')
        if not _idle(connection, task):
            raise board_store.TaskConflictError('Agent context can be cleared only in BACKLOG, WAIT or REVIEW after a confirmed stop')
        if _session_exists(connection, task):
            connection.execute(
                'UPDATE board_tasks SET acp_reset_after_run = '
                '(SELECT MAX(rowid) FROM agent_runs WHERE task_pk = ?) WHERE id = ?',
                (task['id'], task['id']))
            connection.execute(
                "INSERT INTO task_chat_messages(task_pk, role, text, updated_at) VALUES (?, 'system', ?, ?)",
                (task['id'], RESET_MESSAGE, datetime.now().strftime('%Y-%m-%d %H:%M')))
    return board_store.get_chat(task_id)
