import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from unittest.mock import Mock

import pytest

import board_store


def test_list_tasks_waits_for_lock_longer_than_default_timeout():
    board_store.init_database()
    board_store.create_task('LOCK-1', 'Locked task', 'Description', [])
    started = Event()

    def read_tasks():
        started.set()
        return board_store.list_tasks()

    with closing(board_store.connect()) as writer, ThreadPoolExecutor() as pool:
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("UPDATE board_tasks SET title = 'Committed title'")
        pending = pool.submit(read_tasks)
        try:
            assert started.wait(timeout=2)
            # The previous sqlite3 default was five seconds.
            time.sleep(6)
            assert not pending.done(), 'Reading must retry while the database is locked'
        finally:
            writer.commit()
        tasks = pending.result(timeout=5)
    assert len(tasks) == 1
    assert tasks[0]['title'] == 'Committed title'


def test_list_tasks_raises_after_three_ten_second_attempts():
    board_store.init_database()
    with closing(board_store.connect()) as writer:
        writer.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match='database is locked') as error:
            board_store.list_tasks()
        elapsed = time.monotonic() - started
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert 30 <= elapsed < 35
        writer.rollback()
    assert board_store.list_tasks() == []


def test_list_tasks_does_not_retry_unrelated_sql_errors(monkeypatch):
    connect = Mock(wraps=board_store.connect)
    monkeypatch.setattr(board_store, 'connect', connect)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        board_store.list_tasks()
    assert time.monotonic() - started < 2
    connect.assert_called_once_with()


@pytest.mark.parametrize('failures', [0, 1, 2, 3])
@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
def test_list_tasks_retries_whole_snapshot_and_closes_connections(monkeypatch, failures, code):
    board_store.init_database()
    expected = board_store.create_task('RETRY-1', 'Retry task', 'Description', [])
    original_connect = board_store.connect
    original_serialize = board_store._serialize
    connections = []
    errors = []

    def connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    def serialize(connection, row):
        if len(errors) < failures:
            error = sqlite3.OperationalError('database is locked')
            error.sqlite_errorcode = code
            errors.append(error)
            raise error
        return original_serialize(connection, row)

    monkeypatch.setattr(board_store, 'connect', connect)
    monkeypatch.setattr(board_store, '_serialize', serialize)
    if failures == 3:
        with pytest.raises(sqlite3.OperationalError) as caught:
            board_store.list_tasks()
        assert caught.value is errors[-1]
    else:
        assert board_store.list_tasks() == [expected]
    assert len(connections) == min(failures + 1, 3)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            connection.execute('SELECT 1')
