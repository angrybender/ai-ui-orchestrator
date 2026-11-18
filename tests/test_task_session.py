"""Context reset uses a durable boundary; old session IDs remain diagnostic history."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import threading

from fastapi.testclient import TestClient
import pytest

import agent_remote
import agent_store
import board_store
import main
import task_session
from task_cycle import TaskCycle
from test_agent_remote import setup  # noqa: F401 - isolated in-memory SSH fixture


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


def seed(status='REVIEW'):
    board_store.create_task('PRJ-12', 'Session task', 'Current requirements', [])
    board_store.move_task('PRJ-12', 'OPEN', 0)
    run = agent_store.claim(60)
    agent_store.session(run['id'], 'old-session')
    agent_store.message(run['id'], 'Original response')
    agent_store.finish(run['id'])
    board_store.move_task('PRJ-12', status, 0, force=True)
    return run


def db_rows(sql, args=()):
    with closing(board_store.connect()) as db:
        return [dict(row) for row in db.execute(sql, args)]


@pytest.mark.parametrize('status', ['BACKLOG', 'WAIT', 'REVIEW'])
def test_clear_preserves_history_files_status_and_preparation(client, status):
    seed(status)
    with closing(board_store.connect()) as db, db:
        db.execute("UPDATE board_tasks SET context_ready = 1, init_succeeded = 1, "
                   "remote_cwd = '/tasks/PRJ-12', remote_identity = '[\"host\", \"user\"]'")
        # Simulate a clock rollback: old sessions must never outrank newer inserts.
        db.execute('UPDATE agent_runs SET created_at = created_at + 1000000')
    before_task = db_rows('SELECT * FROM board_tasks')[0]
    before_runs = db_rows('SELECT * FROM agent_runs')
    before_chat = board_store.get_chat('PRJ-12')['messages']
    assert board_store.get_chat('PRJ-12')['can_clear_context'] is True
    response = client.post('/api/board/tasks/PRJ-12/clear-context')
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == status
    assert data['can_clear_context'] is False
    assert data['messages'][:-1] == before_chat
    assert data['messages'][-1]['role'] == 'system'
    assert data['messages'][-1]['text'] == task_session.RESET_MESSAGE
    assert db_rows('SELECT * FROM agent_runs') == before_runs
    after_task = db_rows('SELECT * FROM board_tasks')[0]
    assert after_task.pop('acp_reset_after_run') > 0
    before_task.pop('acp_reset_after_run')
    assert after_task == before_task
    assert client.post('/api/board/tasks/PRJ-12/clear-context').json() == data
    # Durable across startup, failure before session/new, and repeated claims.
    board_store.init_database()
    agent_store.init_database()
    for _ in range(2):
        board_store.move_task('PRJ-12', 'OPEN', 0, force=True)
        run = agent_store.claim(60)
        assert run['session_id'] is None
        assert run['init_succeeded'] is True
        agent_store.finish(run['id'], 'Failed before creating a session')
    board_store.add_user_comment('PRJ-12', 'Continue from the saved files')
    fresh = agent_store.claim(60)
    assert fresh['session_id'] is None
    assert fresh['comment'] == 'Continue from the saved files'
    agent_store.session(fresh['id'], 'new-session')
    agent_store.finish(fresh['id'])
    assert board_store.get_chat('PRJ-12')['can_clear_context'] is True
    board_store.add_user_comment('PRJ-12', 'Next instruction')
    assert agent_store.claim(60)['session_id'] == 'new-session'


@pytest.mark.parametrize('status', ['OPEN', 'IN PROGRESS', 'DONE', 'ARCHIVE'])
def test_clear_rejects_other_statuses(client, status):
    seed(status)
    before = board_store.get_chat('PRJ-12')
    assert before['can_clear_context'] is False
    assert client.post('/api/board/tasks/PRJ-12/clear-context').status_code == 409
    assert board_store.get_chat('PRJ-12') == before


@pytest.mark.parametrize('table,field', [('board_tasks', 'init_uncertain'), ('board_tasks', 'agent_uncertain'),
                                        ('agent_runs', 'init_uncertain'), ('agent_runs', 'agent_uncertain')])
def test_clear_does_not_bypass_unconfirmed_stop(client, table, field):
    seed('BACKLOG')
    with closing(board_store.connect()) as db, db:
        db.execute(f'UPDATE {table} SET {field} = 1')
    assert board_store.get_chat('PRJ-12')['can_clear_context'] is False
    assert client.post('/api/board/tasks/PRJ-12/clear-context').status_code == 409


def test_cancelling_run_blocks_clear_even_after_backlog_transition(client):
    seed()
    board_store.add_user_comment('PRJ-12', 'Continue')
    run = agent_store.claim(60)
    board_store.move_task('PRJ-12', 'BACKLOG', 0)
    assert client.post('/api/board/tasks/PRJ-12/clear-context').status_code == 409
    agent_store.finish(run['id'], 'Remote agent cancelled.', stop_reason='cancelled')
    assert client.post('/api/board/tasks/PRJ-12/clear-context').status_code == 200


def test_missing_and_unstarted_tasks(client):
    assert client.post('/api/board/tasks/MISSING/clear-context').status_code == 404
    board_store.create_task('NEW-1', 'New', 'Details', [])
    before = board_store.get_chat('NEW-1')
    assert before['can_clear_context'] is False
    assert client.post('/api/board/tasks/NEW-1/clear-context').json() == before
    assert db_rows('SELECT * FROM agent_runs') == []


def test_concurrent_resets_add_one_boundary_message(client):
    seed()
    barrier = threading.Barrier(2)

    def reset():
        barrier.wait(timeout=5)
        return task_session.clear_context('PRJ-12')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reset(), range(2)))
    assert all(result['can_clear_context'] is False for result in results)
    assert len(board_store.get_chat('PRJ-12')['messages']) == 2


def test_reset_creates_new_acp_session_in_existing_legacy_directory(client, setup, monkeypatch):
    seed()
    task_session.clear_context('PRJ-12')
    board_store.add_user_comment('PRJ-12', 'Check the saved result')
    run = agent_store.claim(60)
    # Legacy tasks have session ownership but no context_ready/init_succeeded.
    assert not run['context_ready'] and not run['init_succeeded']
    assert run['has_session_context'] is True and run['session_id'] is None
    ssh, config, _, _, _, _ = setup
    config['agent.continue_session_arg'] = '--resume-old'
    config['tasks.init_script_text'] = 'must not run'
    ssh.sftp.dirs.update(['/tasks/PRJ-12', '/tasks/PRJ-12/requirements-PRJ-12'])
    ssh.sftp.files['/tasks/PRJ-12/result.txt'] = b'existing result'
    monkeypatch.setattr(agent_remote, 'run_init', lambda *a, **k: pytest.fail('Init reran after clear'))
    result = agent_remote.run_remote(
        {'task_id': run['task_id'], 'session_id': run['session_id'], 'comment': run['comment'],
         'title': 'Current title', 'description': 'Current requirements', '_cycle': TaskCycle(run, config)}, [], config,
        lambda value: agent_store.session(run['id'], value), lambda _: None, lambda: None)
    assert result['stopReason'] == 'end_turn'
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [request['method'] for request in requests] == ['initialize', 'session/new', 'session/prompt']
    assert requests[1]['params']['cwd'] == '/tasks/PRJ-12'
    assert requests[2]['params']['prompt'][0]['text'] == agent_remote.task_prompt('PRJ-12') + '\n\nCheck the saved result'
    assert '--resume-old' not in ssh.channel.command
    assert ssh.sftp.files['/tasks/PRJ-12/result.txt'] == b'existing result'
    assert 'Current requirements' in ssh.sftp.files['/tasks/PRJ-12/requirements-PRJ-12/TASK.md'].decode()
