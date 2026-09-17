"""Real ACP subprocesses and descendants; SSH and storage remain isolated."""
import io
import json
import threading
import time

import pytest

import agent_control
import agent_remote
import agent_store
import board_store
from agent_cancel_fixture import Harness, alive
from task_cycle import TaskCycle


@pytest.mark.parametrize('mode', ['cooperative', 'delayed', 'race', 'ignore'])
@pytest.mark.parametrize('transition', ['form', 'move'])
def test_cancel_stops_real_agent_and_descendant(tmp_path, monkeypatch, mode, transition):
    harness = Harness(tmp_path, monkeypatch, mode)
    monkeypatch.setattr(agent_remote, 'CANCEL_TIMEOUT', 0.5)
    try:
        harness.start()
        run = agent_store.active_run()
        pid = run['agent_pid']
        child = int((harness.cwd / 'child.pid').read_text())
        assert alive(pid) and alive(child)
        started = time.monotonic()
        if transition == 'form':
            board_store.update_task('CANCEL-1', 'Cancel a running agent', 'Details', 'BACKLOG', [], [])
        else:
            board_store.move_task('CANCEL-1', 'BACKLOG', 0)
        assert agent_store.active_run()['cancel_requested_at'] is not None
        assert agent_store.claim(60) is None
        harness.worker.join(5)
        assert not harness.worker.is_alive()
        assert (harness.cwd / 'cancel.received').exists()
        assert not alive(pid) and not alive(child)
        assert harness.ssh.closed
        assert all(channel.closed for channel in harness.ssh.channels)
        assert time.monotonic() - started < 4
        if mode == 'ignore':
            assert time.monotonic() - started >= 0.5
        assert board_store.get_task('CANCEL-1')['status'] == 'BACKLOG'
        with board_store.connect() as db:
            saved = dict(db.execute('SELECT * FROM agent_runs WHERE id = ?', (run['id'],)).fetchone())
        assert saved['state'] == 'FAILED'
        assert saved['stop_reason'] == 'cancelled'
        assert saved['agent_stop_confirmed'] == 1
        assert saved['agent_uncertain'] == 0
        assert agent_store.active_run() is None
        assert all('late reply' not in m['text'] for m in board_store.get_chat('CANCEL-1')['messages'])
        board_store.move_task('CANCEL-1', 'OPEN', 0)
        replacement = agent_store.claim(60)
        assert replacement and replacement['id'] != run['id']
        agent_store.finish(run['id'], 'Late old error')
        assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
        agent_store.finish(replacement['id'], 'Fixture cleanup')
    finally:
        harness.cleanup()


def test_unconfirmed_stop_blocks_queue_and_reports_system_message(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_control, 'stop_group', lambda *_: False)
    # The SSH fake must unblock local readers even when confirmation is lost.
    original_stop = agent_control.AgentControl.stop
    def stop(control):
        import os, signal
        os.killpg(control.pid, signal.SIGKILL)
        return original_stop(control)
    monkeypatch.setattr(agent_control.AgentControl, 'stop', stop)
    try:
        harness.start()
        board_store.move_task('CANCEL-1', 'BACKLOG', 0)
        harness.worker.join(5)
        assert not harness.worker.is_alive()
        assert board_store.get_task('CANCEL-1')['status'] == 'BACKLOG'
        chat = board_store.get_chat('CANCEL-1')['messages']
        assert chat[-1]['role'] == 'system'
        assert 'Remote stop unconfirmed; retry blocked' in chat[-1]['text']
        board_store.move_task('CANCEL-1', 'OPEN', 0)
        board_store.create_task('CANCEL-2', 'Other task', 'Details', [])
        board_store.move_task('CANCEL-2', 'OPEN', 0)
        assert agent_store.claim(60) is None
    finally:
        harness.cleanup()


def test_cancel_grace_is_sixty_seconds_and_recovery_honours_it(monkeypatch):
    assert agent_remote.CANCEL_TIMEOUT == agent_control.CANCEL_TIMEOUT == 60
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('T-1', 'Task', 'Details', [])
    board_store.move_task('T-1', 'OPEN', 0)
    run = agent_store.claim(1)
    cycle = TaskCycle(run, {'agent.agent_timeout': 1})
    cycle.phase('Agent')
    board_store.move_task('T-1', 'BACKLOG', 0)
    row = agent_store.active_run()
    now = row['cancel_requested_at']
    monkeypatch.setattr(agent_store.time, 'time', lambda: now + 59.9)
    assert not agent_store._expired(row)
    monkeypatch.setattr(agent_store.time, 'time', lambda: now + 60 + agent_control.STOP_GRACE + 1)
    assert agent_store._expired(row)


def test_dead_executor_leaves_queue_blocked_until_remote_stop_verified(monkeypatch):
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('T-1', 'Task', 'Details', [])
    board_store.move_task('T-1', 'OPEN', 0)
    run = agent_store.claim(120)
    TaskCycle(run, {'agent.agent_timeout': 120}).phase('Agent')
    board_store.move_task('T-1', 'BACKLOG', 0)
    monkeypatch.setattr(agent_store, 'process_identity', lambda _: None)
    agent_store.recover()
    assert board_store.get_task('T-1')['status'] == 'BACKLOG'
    board_store.move_task('T-1', 'OPEN', 0)
    assert agent_store.claim(120) is None


def test_cancel_before_handshake_never_opens_gate():
    stopped = threading.Event()
    control = agent_control.AgentControl(None, stopped, lambda _: None)
    control.requested.set()
    stdin = io.BytesIO()
    assert not control.handshake(stdin, io.BytesIO(b'ACP_PID:12345\n'))
    assert stdin.getvalue() == b''
    assert control.stop()


def test_cancel_serializes_with_outgoing_prompt():
    entered, release = threading.Event(), threading.Event()
    class SlowInput(io.BytesIO):
        def write(self, value):
            if b'session/prompt' in value:
                entered.set()
                assert release.wait(2)
            return super().write(value)
    stdin = SlowInput()
    control = agent_control.AgentControl(None, threading.Event(), lambda _: None)
    control.handshake(stdin, io.BytesIO(b'ACP_PID:12345\n'))
    writer = threading.Thread(target=control.send, args=({'jsonrpc': '2.0', 'id': 3,
        'method': 'session/prompt', 'params': {'sessionId': 's'}},))
    writer.start()
    assert entered.wait(1)
    canceller = threading.Thread(target=control.request_cancel)
    canceller.start()
    release.set()
    writer.join(2)
    canceller.join(2)
    messages = [json.loads(line) for line in stdin.getvalue().splitlines()[1:]]
    assert [m['method'] for m in messages] == ['session/prompt', 'session/cancel']
    assert messages[-1] == {'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': 's'}}


def test_cancel_before_session_stops_without_sixty_second_wait(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, 'before-session')
    try:
        harness.worker.start()
        deadline = time.monotonic() + 5
        while not (harness.cwd / 'initialize.ready').exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = agent_store.active_run()['agent_pid']
        started = time.monotonic()
        board_store.move_task('CANCEL-1', 'BACKLOG', 0)
        harness.worker.join(4)
        assert not harness.worker.is_alive()
        assert time.monotonic() - started < 4
        assert not alive(pid)
        assert not (harness.cwd / 'cancel.received').exists()
        assert board_store.get_task('CANCEL-1')['status'] == 'BACKLOG'
        assert harness.ssh.closed
    finally:
        harness.cleanup()


def test_blocked_cancel_write_cannot_extend_deadline(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    original = agent_control.AgentControl._write
    attempted = threading.Event()
    def write(control, message):
        if message.get('method') == 'session/cancel':
            attempted.set()
            assert control.cancelled.wait(3)
            raise OSError('SSH write blocked until close')
        return original(control, message)
    monkeypatch.setattr(agent_control.AgentControl, '_write', write)
    monkeypatch.setattr(agent_remote, 'CANCEL_TIMEOUT', 0.3)
    try:
        harness.start()
        pid = agent_store.active_run()['agent_pid']
        started = time.monotonic()
        board_store.move_task('CANCEL-1', 'BACKLOG', 0)
        harness.worker.join(4)
        assert attempted.is_set()
        assert not harness.worker.is_alive()
        assert 0.3 <= time.monotonic() - started < 4
        assert not alive(pid)
        assert harness.ssh.closed
    finally:
        harness.cleanup()
