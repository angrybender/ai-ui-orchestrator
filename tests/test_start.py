import argparse
import subprocess
import threading
from unittest.mock import Mock

import pytest

import agent_consumer
import start


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_interval_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        agent_consumer.positive_interval(value)


def test_consumer_runs_fresh_worker_after_failure(monkeypatch):
    stop = threading.Event()
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 2:
            stop.set()
        return Mock(poll=Mock(return_value=1 if len(calls) == 1 else 0))

    monkeypatch.setattr(agent_consumer.subprocess, 'Popen', spawn)
    assert agent_consumer.consume(stop, 0.001) == 0
    assert len(calls) == 2
    assert all(command[-1].endswith('agent_worker.py') for command, _ in calls)
    assert all(kwargs['start_new_session'] for _, kwargs in calls)


def test_consumer_stops_running_worker(monkeypatch):
    stop = threading.Event()
    process = Mock(poll=Mock(return_value=None))

    def spawn(*args, **kwargs):
        stop.set()
        return process

    monkeypatch.setattr(agent_consumer.subprocess, 'Popen', spawn)
    assert agent_consumer.consume(stop, 1) == 0
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=10)


def test_consumer_retries_spawn_failure_without_leaking_error(monkeypatch, caplog):
    stop = threading.Event()

    def spawn(*args, **kwargs):
        stop.set()
        raise OSError('secret')

    monkeypatch.setattr(agent_consumer.subprocess, 'Popen', spawn)
    assert agent_consumer.consume(stop, 1) == 0
    assert 'secret' not in caplog.text


def test_stop_escalates_unresponsive_child():
    process = Mock(poll=Mock(return_value=None))
    process.wait.side_effect = [subprocess.TimeoutExpired('worker', 10), 0]
    agent_consumer.stop_process(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_supervisor_starts_both_and_stops_them(monkeypatch):
    stop = threading.Event()
    calls = []
    children = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        child = Mock(poll=Mock(return_value=None))
        children.append(child)
        if len(children) == 2:
            stop.set()
        return child

    monkeypatch.setattr(start.subprocess, 'Popen', spawn)
    assert start.serve(stop, '127.0.0.1', 9000, 2) == 0
    assert calls[0][0][1:] == ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '9000']
    assert calls[0][1]['env']['APP_WEB_HOST'] == '127.0.0.1'
    assert calls[0][1]['env']['APP_WEB_PORT'] == '9000'
    assert calls[1][0][-2:] == ['--poll-interval', '2']
    for child in children:
        child.terminate.assert_called_once()


def test_supervisor_cleans_up_when_second_spawn_fails(monkeypatch):
    child = Mock(poll=Mock(return_value=None))
    monkeypatch.setattr(start.subprocess, 'Popen', Mock(side_effect=[child, OSError('failed')]))
    assert start.serve(threading.Event(), '127.0.0.1', 9000, 2) == 1
    child.terminate.assert_called_once()


def test_supervisor_stops_peer_on_unexpected_exit(monkeypatch):
    dead = Mock(poll=Mock(return_value=0))
    alive = Mock(poll=Mock(return_value=None))
    monkeypatch.setattr(start.subprocess, 'Popen', Mock(side_effect=[dead, alive]))
    assert start.serve(threading.Event(), '127.0.0.1', 9000, 2) == 1
    alive.terminate.assert_called_once()
