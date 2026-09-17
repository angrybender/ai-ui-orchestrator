"""A shared database must never equate a foreign PID with a dead executor."""
import pytest
from types import SimpleNamespace

import agent_launcher
import agent_store
import board_store
from task_cycle import TaskCycle


@pytest.fixture
def run():
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('TASK-17', 'Task', 'Details', [])
    board_store.move_task('TASK-17', 'OPEN', 0)
    result = agent_store.claim(60)
    TaskCycle(result, {'agent.agent_timeout': 60}).phase('Agent')
    return result


@pytest.mark.parametrize('expired', [False, True])
def test_foreign_executor_cannot_be_recovered_or_replaced(run, monkeypatch, expired):
    original = agent_store.active_run()
    assert original['executor_scope'] == agent_store.executor_scope()
    monkeypatch.setattr(agent_store, 'executor_scope', lambda: 'another-machine-or-namespace')
    monkeypatch.setattr(agent_store, 'process_identity', lambda _: pytest.fail('Foreign PID was inspected'))
    monkeypatch.setattr(agent_store, '_expired', lambda _: expired)
    agent_store.recover()
    assert agent_store.active_run() == original
    assert agent_store.claim(60) is None
    assert agent_launcher.launch() is None
    assert board_store.get_task('TASK-17')['status'] == 'IN PROGRESS'


@pytest.mark.parametrize('system,token', [('posix', '134341333962054724'), ('nt', 'boot-uuid:1234')])
def test_legacy_foreign_owner_is_not_dead(run, monkeypatch, system, token):
    with board_store.connect() as db:
        db.execute('UPDATE agent_runs SET executor_scope = NULL, process_token = ?', (token,))
    monkeypatch.setattr(agent_store, 'os', SimpleNamespace(name=system))
    monkeypatch.setattr(agent_store, 'process_identity', lambda _: pytest.fail('Foreign PID was inspected'))
    agent_store.recover()
    assert agent_store.active_run()['id'] == run['id']


def test_local_dead_owner_is_still_recovered(run, monkeypatch):
    monkeypatch.setattr(agent_store, 'process_identity', lambda _: None)
    agent_store.recover()
    assert agent_store.active_run() is None
    assert board_store.get_task('TASK-17')['status'] == 'WAIT'


def test_recovery_snapshot_cannot_overwrite_new_stop_confirmation(run):
    stale = agent_store.active_run()
    TaskCycle(run, {}).agent_stopped(True)
    agent_store._finish_recovered(stale, 'Agent executor terminated.')
    with board_store.connect() as db:
        assert db.execute('SELECT agent_uncertain FROM agent_runs WHERE id=?', (run['id'],)).fetchone()[0] == 0
        assert db.execute("SELECT agent_uncertain FROM board_tasks WHERE task_id='TASK-17'").fetchone()[0] == 0


@pytest.mark.parametrize('another_uncertain', [False, True])
def test_late_stop_confirmation_reconciles_task_flag(run, another_uncertain):
    agent_store.finish(run['id'], 'Agent executor terminated.', agent_uncertain=True)
    if another_uncertain:
        with board_store.connect() as db:
            db.execute("INSERT INTO agent_runs(id, task_pk, state, pid, process_token, created_at, timeout, agent_uncertain) "
                       "SELECT 'other', task_pk, 'FAILED', pid, process_token, created_at, timeout, 1 FROM agent_runs WHERE id=?", (run['id'],))
    TaskCycle(run, {}).agent_stopped(True)
    with board_store.connect() as db:
        assert db.execute("SELECT agent_uncertain FROM board_tasks WHERE task_id='TASK-17'").fetchone()[0] == int(another_uncertain)
        saved = db.execute('SELECT agent_uncertain,agent_stop_confirmed,state FROM agent_runs WHERE id=?', (run['id'],)).fetchone()
        assert tuple(saved) == (0, 1, 'FAILED')
    assert board_store.get_task('TASK-17')['status'] == 'WAIT'
