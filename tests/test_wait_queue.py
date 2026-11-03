"""Real-process WAIT continuation with isolated SQLite and local transport."""
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sys
import sqlite3
import time
from types import SimpleNamespace

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
    processes = fixture.configure_launcher()
    patched = agent_launcher.subprocess.Popen
    agent_launcher.subprocess.Popen = original
    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', patched)
    try:
        yield fixture, processes
    finally:
        fixture.stop_test_workers(processes)


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


@pytest.mark.parametrize('failure', [agent_worker.RemoteAgentError, agent_worker.InitError])
@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit, sqlite3.OperationalError])
def test_stop_during_failure_persistence_cannot_start_successor(queue, monkeypatch, failure, interruption):
    seed(('success', 'failure', 'success'))
    original_finish = agent_store.finish
    launches = []

    def fail_transport(*args, **kwargs):
        raise failure('Original error')

    def interrupted_finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        # A signal can arrive just after SQLite committed WAIT, including in
        # an except handler which the worker's sibling handlers cannot catch.
        raise interruption()

    monkeypatch.setattr(agent_worker, 'run_remote', fail_transport)
    monkeypatch.setattr(agent_store, 'finish', interrupted_finish)
    monkeypatch.setattr(agent_launcher, 'launch', lambda **kwargs: launches.append(kwargs))
    with pytest.raises(interruption):
        agent_worker.execute(continue_after_wait=True)
    assert board_store.get_task('Q-9')['status'] == 'WAIT'
    assert launches == []


@pytest.mark.parametrize('outcome', [None, 'Consumer failed'])
def test_delayed_successor_does_not_skip_past_consumer_run(queue, monkeypatch, outcome):
    seed(('success', 'failure', 'success'))
    predecessor = agent_store.claim(60)
    agent_store.finish(predecessor['id'], 'Original error')
    dispatched = []

    def delayed_popen(command, **kwargs):
        dispatched.append(command)
        return SimpleNamespace(pid=12345, wait=lambda: 0)

    monkeypatch.setattr(agent_launcher.subprocess, 'Popen', delayed_popen)
    assert agent_launcher.launch(after_wait=predecessor['id']) == 12345
    consumer_run = agent_store.claim(60)
    assert consumer_run['task_id'] == 'Q-1'
    agent_store.finish(consumer_run['id'], outcome)
    # Complete the competing run before the dispatched child has even claimed
    # a task. PREPARING/RUNNING uniqueness alone cannot fence this race.
    transports = []
    monkeypatch.setattr(agent_worker, 'run_remote', lambda task, *a, **k: transports.append(task['task_id']) or {'stopReason': 'end_turn'})
    monkeypatch.setattr(agent_worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', dispatched[0][1:])
    assert agent_worker.main() == 0
    assert transports == []
    assert board_store.get_task('Q-10')['status'] == 'OPEN'
    with closing(board_store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 2


def test_continuation_uses_claim_order_when_clock_moves_backwards(queue, monkeypatch):
    seed(('success', 'failure', 'success'))
    first = agent_store.claim(60)
    agent_store.finish(first['id'], 'First error')
    second = agent_store.claim(60)
    agent_store.finish(second['id'], 'Second error')
    with closing(board_store.connect()) as db, db:
        db.execute('UPDATE agent_runs SET created_at = 0 WHERE id = ?', (second['id'],))
    assert agent_store.claim(60, after_wait=first['id']) is None
    continuation = agent_store.claim(60, after_wait=second['id'])
    assert continuation['task_id'] == 'Q-10'
    agent_store.finish(continuation['id'])


def test_teardown_never_signals_pytest_or_unverified_pid(queue, monkeypatch):
    fixture, _ = queue
    seed(('success', 'failure', 'success'))
    run = agent_store.claim(60)
    original_scope = agent_store.executor_scope()

    def unexpected_signal(*args, **kwargs):
        pytest.fail('Fixture tried to signal an unowned process')

    monkeypatch.setattr(fixture.os, 'kill', unexpected_signal)
    monkeypatch.setattr(fixture, 'stop_process', unexpected_signal)
    fixture.stop_test_workers([])  # A claim in this interpreter belongs to pytest.
    with closing(board_store.connect()) as db, db:
        db.execute("UPDATE agent_runs SET pid = 12345, executor_scope = 'foreign' WHERE id = ?", (run['id'],))
    fixture.stop_test_workers([])
    with closing(board_store.connect()) as db, db:
        db.execute('UPDATE agent_runs SET executor_scope = ? WHERE id = ?', (original_scope, run['id']))
    monkeypatch.setattr(agent_store, 'process_identity', lambda _: 'reused-pid-token')
    fixture.stop_test_workers([])
    agent_store.finish(run['id'], 'Test cleanup')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX SIGTERM delivery; Windows interruption is tested separately')
def test_real_sigterm_after_wait_commit_does_not_start_next_task(queue):
    _, processes = queue
    seed(('success', 'sigterm-at-finish', 'success'))
    assert agent_launcher.launch()
    assert processes[0].wait(timeout=15) == 1
    assert board_store.get_task('Q-9')['status'] == 'WAIT'
    assert board_store.get_task('Q-1')['status'] == 'OPEN'
    assert board_store.get_task('Q-10')['status'] == 'OPEN'
    with closing(board_store.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 1
