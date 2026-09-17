"""Recovery of a legacy DONE task blocking a fresh OPEN task at startup."""
from fastapi.testclient import TestClient
import main

import pytest

import agent_store
import board_store


@pytest.fixture
def blocked():
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('TASK-15', 'Previous task', 'Description', [])
    board_store.move_task('TASK-15', 'OPEN', 0)
    run = agent_store.claim(60)
    with board_store.connect() as db:
        db.execute("UPDATE agent_runs SET phase = 'Agent' WHERE id = ?", (run['id'],))
    board_store.move_task('TASK-15', 'DONE', 0, force=True)
    agent_store.finish(run['id'], 'Agent executor terminated.', agent_uncertain=True)
    board_store.create_task('TASK-16', 'Next task', 'Description', [])
    board_store.move_task('TASK-16', 'OPEN', 0)
    return run['id']


def test_startup_block_is_reported_and_done_does_not_bypass_it(blocked, caplog):
    board_store.init_database()
    agent_store.init_database()
    assert agent_store.claim(60) is None
    assert board_store.get_task('TASK-16')['status'] == 'OPEN'
    assert 'TASK-15' in caplog.text
    assert blocked in caplog.text
    assert 'Agent Recovery on Board' in caplog.text


def test_operator_confirmation_allows_task16_and_preserves_history(blocked):
    original_chat = board_store.get_chat('TASK-15')
    agent_store.confirm_remote_stop(blocked)
    with board_store.connect() as db:
        run = dict(db.execute('SELECT * FROM agent_runs WHERE id = ?', (blocked,)).fetchone())
    assert run['state'] == 'FAILED'
    assert run['agent_pid'] is None  # Legacy runs have no group handshake.
    assert run['agent_uncertain'] == 0
    assert run['agent_stop_confirmed'] == 1
    assert 'Agent executor terminated.' in run['error']
    assert 'verified by operator' in run['log']
    assert board_store.get_chat('TASK-15') == original_chat
    claimed = agent_store.claim(60)
    assert claimed['task_id'] == 'TASK-16'
    assert board_store.get_task('TASK-16')['status'] == 'IN PROGRESS'
    assert board_store.get_task('TASK-15')['status'] == 'DONE'


def test_confirmation_does_not_clear_another_uncertain_run(blocked):
    with board_store.connect() as db:
        db.execute("INSERT INTO agent_runs(id, task_pk, state, pid, process_token, created_at, timeout, agent_uncertain) "
                   "SELECT 'other', task_pk, 'FAILED', pid, process_token, created_at, timeout, 1 FROM agent_runs WHERE id = ?", (blocked,))
    agent_store.confirm_remote_stop(blocked)
    assert agent_store.claim(60) is None
    with board_store.connect() as db:
        assert db.execute("SELECT agent_uncertain FROM board_tasks WHERE task_id = 'TASK-15'").fetchone()[0] == 1
    agent_store.confirm_remote_stop('other')
    assert agent_store.claim(60)['task_id'] == 'TASK-16'


def test_confirmation_rejects_active_unknown_and_already_confirmed(blocked):
    with pytest.raises(ValueError, match='Unknown'):
        agent_store.confirm_remote_stop('missing')
    agent_store.confirm_remote_stop(blocked)
    with pytest.raises(ValueError, match='no unconfirmed'):
        agent_store.confirm_remote_stop(blocked)
    active = agent_store.claim(60)
    with pytest.raises(ValueError, match='active'):
        agent_store.confirm_remote_stop(active['id'])


def test_recovery_endpoint_preserves_history_and_handles_repeated_request(blocked):
    history = board_store.get_chat('TASK-15')
    with TestClient(main.app) as client:
        assert client.get('/api/agent/recovery').json() == {'runs': [{'run_id': blocked, 'task_id': 'TASK-15'}]}
        result = client.post('/api/agent/recovery', json={'run_id': blocked})
        assert result.status_code == 200
        assert result.json() == {'run_id': blocked, 'remaining': 0}
        assert client.post('/api/agent/recovery', json={'run_id': blocked}).status_code == 409
        assert client.get('/api/agent/recovery').json() == {'runs': []}
    assert board_store.get_chat('TASK-15') == history
    assert agent_store.claim(60)['task_id'] == 'TASK-16'


def test_recovery_api_rejects_active_unknown_and_missing_run(blocked):
    agent_store.confirm_remote_stop(blocked)
    active = agent_store.claim(60)
    with TestClient(main.app) as client:
        for run_id in (active['id'], 'unknown'):
            assert client.post('/api/agent/recovery', json={'run_id': run_id}).status_code == 409
        assert client.post('/api/agent/recovery', json={}).status_code == 422
    assert agent_store.active_run()['id'] == active['id']
