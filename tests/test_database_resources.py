"""SQLite resources must not depend on cyclic garbage collection."""
import sqlite3
import subprocess
import sys

import pytest

import board_store


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux descriptor limit regression')
@pytest.mark.parametrize('operation', ['chat', 'agent'])
def test_polling_with_low_descriptor_limit(tmp_path, operation):
    # Limit only the child; delayed GC makes descriptor lifetime deterministic.
    result = subprocess.run([sys.executable, '-c', r'''
import gc
import os
import resource
import sys
from pathlib import Path
import board_store
import agent_store
from task_cycle import TaskCycle

board_store.DATABASE_PATH = Path(sys.argv[1]) / 'app.db'
board_store.FILES_DIR = Path(sys.argv[1]) / 'files'
board_store.init_database()
agent_store.init_database()
board_store.create_task('FD-1', 'Descriptor regression', 'Test', [])
board_store.move_task('FD-1', 'OPEN', 0)
run = agent_store.claim(7200)
cycle = TaskCycle(run, {'agent.agent_timeout': 7200})
cycle.phase('Agent')
agent_store.started(run['id'])
gc.collect()
gc.disable()
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(96, soft), hard))
baseline = len(os.listdir('/proc/self/fd'))
for index in range(2000):
    if sys.argv[2] == 'chat':
        assert board_store.get_chat('FD-1')['status'] == 'IN PROGRESS'
    else:
        assert cycle.active()
        assert agent_store.active_run()['id'] == run['id']
        if index % 20 == 0:
            agent_store.message(run['id'], str(index))
            agent_store.append_log(run['id'], 'tick\n')
assert len(os.listdir('/proc/self/fd')) <= baseline + 2
cycle.agent_stopped(True)
agent_store.finish(run['id'], stop_reason='end_turn')
assert board_store.get_task('FD-1')['status'] == 'REVIEW'
assert agent_store.active_run() is None
''', str(tmp_path), operation],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_write_failure_rolls_back_and_closes_connection(monkeypatch):
    board_store.init_database()
    original = board_store.connect
    connections = []

    def connect():
        connection = original()
        connections.append(connection)
        return connection

    def fail(*args):
        raise OSError('Attachment write failed')

    monkeypatch.setattr(board_store, 'connect', connect)
    monkeypatch.setattr(board_store, '_save_files', fail)
    with pytest.raises(OSError, match='Attachment write failed'):
        board_store.create_task('FD-1', 'Rollback', 'Test', [])
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            connection.execute('SELECT 1')
    assert board_store.list_tasks() == []
