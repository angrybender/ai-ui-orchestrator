from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

import agent_consumer
import agent_store
import board_store
import windows_process


@pytest.fixture
def windows_store(monkeypatch):
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('WIN-1', 'Windows task', 'Description', [])
    board_store.move_task('WIN-1', 'OPEN', 0)
    monkeypatch.setattr(agent_store, 'os', SimpleNamespace(name='nt', getpid=lambda: 100))
    monkeypatch.setattr(windows_process, 'process_identity', lambda pid: 'creation-1')
    return agent_store.claim(60)


def test_windows_claim_retains_monopoly(windows_store):
    assert agent_store.active_run()['process_token'] == 'creation-1'
    assert board_store.get_task('WIN-1')['status'] == 'IN PROGRESS'
    assert agent_store.claim(60) is None


@pytest.mark.parametrize('confirmed', [True, False])
def test_windows_expired_recovery_waits_for_confirmation(windows_store, monkeypatch, confirmed):
    with board_store.connect() as connection:
        connection.execute('UPDATE agent_runs SET pid = 200, created_at = ?', (time.time() - 120,))
    terminate = Mock(return_value=confirmed)
    monkeypatch.setattr(windows_process, 'terminate_process', terminate)
    agent_store.recover()
    terminate.assert_called_once_with(200, 'creation-1')
    assert (agent_store.active_run() is None) == confirmed
    assert board_store.get_task('WIN-1')['is_error'] == confirmed


def test_windows_access_denied_preserves_active_run(windows_store, monkeypatch):
    monkeypatch.setattr(windows_process, 'process_identity', Mock(side_effect=PermissionError()))
    agent_store.recover()
    assert agent_store.active_run()['id'] == windows_store['id']


def test_windows_dead_process_releases_task(windows_store, monkeypatch):
    monkeypatch.setattr(windows_process, 'process_identity', lambda pid: None)
    agent_store.recover()
    task = board_store.get_task('WIN-1')
    assert task['status'] == 'WAIT' and task['is_error']


def test_windows_process_group_and_graceful_stop(monkeypatch):
    monkeypatch.setattr(agent_consumer, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(agent_consumer.subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, raising=False)
    monkeypatch.setattr(agent_consumer.signal, 'CTRL_BREAK_EVENT', 1, raising=False)
    assert agent_consumer.process_options() == {'creationflags': 512}
    child = Mock(poll=Mock(return_value=None))
    agent_consumer.stop_process(child)
    child.send_signal.assert_called_once_with(1)
    child.terminate.assert_not_called()


def test_windows_no_console_falls_back_to_terminate(monkeypatch):
    monkeypatch.setattr(agent_consumer, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(agent_consumer.signal, 'CTRL_BREAK_EVENT', 1, raising=False)
    child = Mock(poll=Mock(return_value=None), send_signal=Mock(side_effect=OSError()))
    agent_consumer.stop_process(child)
    child.terminate.assert_called_once()
