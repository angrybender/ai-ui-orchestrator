"""Real-process WAIT continuation with isolated SQLite and local transport."""
from contextlib import closing
import importlib.util
from pathlib import Path
import time

import pytest

import agent_launcher
import agent_store
import agent_worker
import board_store


@pytest.fixture
def queue(monkeypatch):
    board_store.init_database()
    agent_store.init_database()
    fixture_path = Path(__file__).with_name('wait_queue_worker.py')
    spec = importlib.util.spec_from_file_location('wait_queue_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    original = agent_launcher.subprocess.Popen
    fixture.configure_launcher()
    patched = agent_launcher.subprocess.Popen
    agent_launcher.subprocess.Popen = original
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', patched)
    yield
    # No worker can outlive its test, including assertion failures.
    with closing(board_store.connect()) as db:
        pids = {row[0] for row in db.execute(f'SELECT pid FROM agent_runs WHERE {agent_store.ACTIVE}')}
    for pid in pids:
        import os
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def seed(modes):
    # Selection must use sort_order, then internal id, rather than task ID.
    for task_id, order, mode in zip(('Q-10', 'Q-9', 'Q-1'), (8, 1, 1), modes):
        board_store.create_task(task_id, task_id, mode, [])
        with closing(board_store.connect()) as db, db:
            db.execute("UPDATE board_tasks SET status = 'OPEN', sort_order = ? WHERE task_id = ?", (order, task_id))


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail('Queue did not reach the expected state')


@pytest.mark.parametrize('failure', ['failure', 'init-error', 'timeout', 'refusal'])
def test_wait_starts_next_open_in_order_without_consumer(queue, failure):
    seed(('success', failure, failure))
    assert agent_launcher.launch()
    wait_for(lambda: board_store.get_task('Q-10')['status'] == 'REVIEW')
    tasks = {task['task_id']: task for task in board_store.list_tasks()}
    assert tasks['Q-9']['status'] == tasks['Q-1']['status'] == 'WAIT'
    assert tasks['Q-9']['is_error'] and tasks['Q-1']['is_error']
    with closing(board_store.connect()) as db:
        runs = db.execute('SELECT r.*, t.task_id FROM agent_runs r JOIN board_tasks t ON t.id = r.task_pk ORDER BY created_at').fetchall()
    assert [run['task_id'] for run in runs] == ['Q-9', 'Q-1', 'Q-10']
    assert [run['state'] for run in runs] == ['FAILED', 'FAILED', 'SUCCEEDED']
    assert len({run['pid'] for run in runs}) == 3
    assert all(previous['finished_at'] <= following['created_at'] for previous, following in zip(runs, runs[1:]))
    assert agent_store.active_run() is None
    assert board_store.get_chat('Q-9')['messages']


def test_uncertain_remote_stop_keeps_queue_blocked(queue):
    seed(('success', 'uncertain', 'success'))
    assert agent_launcher.launch()
    wait_for(lambda: board_store.get_task('Q-9')['status'] == 'WAIT')
    with closing(board_store.connect()) as db:
        run = db.execute('SELECT * FROM agent_runs').fetchone()
    assert agent_launcher.launch(after_wait=run['id']) is None
    assert agent_store.claim(60) is None
    assert board_store.get_task('Q-1')['status'] == 'OPEN'
    assert board_store.get_task('Q-10')['status'] == 'OPEN'


def test_empty_queue_does_not_spawn_another_process(queue, monkeypatch):
    seed(('failure', 'failure', 'failure'))
    assert agent_launcher.launch()
    wait_for(lambda: board_store.get_task('Q-10')['status'] == 'WAIT')
    with closing(board_store.connect()) as db:
        run = db.execute('SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT 1').fetchone()
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unexpected executor'))
    assert agent_launcher.launch(after_wait=run['id']) is None
    assert agent_store.active_run() is None


def test_followup_spawn_failure_preserves_original_failure(queue, monkeypatch, caplog):
    seed(('success', 'failure', 'success'))

    def transport(*args, **kwargs):
        raise agent_worker.RemoteAgentError('Original task error')

    def failed_spawn(*args, **kwargs):
        raise OSError('private details')

    monkeypatch.setattr(agent_worker, 'run_remote', transport)
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', failed_spawn)
    assert agent_worker.execute(continue_after_wait=True) == 1
    assert board_store.get_task('Q-9')['status'] == 'WAIT'
    with closing(board_store.connect()) as db:
        assert db.execute('SELECT error FROM agent_runs').fetchone()[0] == 'Original task error'
    assert board_store.get_task('Q-1')['status'] == 'OPEN'
    assert 'Unable to continue' in caplog.text
    assert 'private details' not in caplog.text


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_stopping_worker_does_not_spawn_successor(queue, monkeypatch, interruption):
    seed(('success', 'failure', 'success'))

    def interrupted(*args, **kwargs):
        raise interruption()

    monkeypatch.setattr(agent_worker, 'run_remote', interrupted)
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unexpected executor'))
    assert agent_worker.execute(continue_after_wait=True) == 1
    assert board_store.get_task('Q-1')['status'] == 'OPEN'


@pytest.mark.parametrize('status,owner', [('BACKLOG', None), ('REVIEW', None), ('OPEN', None), ('WAIT', 'new-run')])
def test_followup_requires_unchanged_wait_owned_by_finished_run(queue, monkeypatch, status, owner):
    seed(('success', 'failure', 'success'))
    run = agent_store.claim(60)
    agent_store.finish(run['id'], 'Original error')
    with closing(board_store.connect()) as db, db:
        db.execute('UPDATE board_tasks SET status = ?, active_run_id = ? WHERE task_id = ?',
                   (status, owner, run['task_id']))
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unexpected executor'))
    assert agent_launcher.launch(after_wait=run['id']) is None


def test_consumer_claiming_next_task_prevents_duplicate_executor(queue, monkeypatch):
    seed(('success', 'failure', 'success'))
    run = agent_store.claim(60)
    agent_store.finish(run['id'], 'Original error')
    successor = agent_store.claim(60)
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Duplicate executor'))
    try:
        assert agent_launcher.launch(after_wait=run['id']) is None
        assert agent_store.active_run()['id'] == successor['id']
    finally:
        agent_store.finish(successor['id'])
