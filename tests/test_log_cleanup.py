import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import log_cleanup
import main
from settings import settings as settings_store
from settings.config import Config


def aged_file(path, timestamp):
    path.write_text('session log')
    os.utime(path, (timestamp, timestamp))
    return path


def test_cleanup_age_boundary_and_safe_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(log_cleanup.time, 'time', lambda: 100000)
    logs = tmp_path / 'logs'
    logs.mkdir()
    old = aged_file(logs / 'old.log', 13599)
    boundary = aged_file(logs / 'boundary.log', 13600)
    fresh = aged_file(logs / 'fresh.log', 99999)
    hidden = aged_file(logs / '.gitignore', 1)
    outside = aged_file(tmp_path / 'outside.log', 1)
    (logs / 'link.log').symlink_to(outside)
    (logs / 'nested').mkdir()
    nested = aged_file(logs / 'nested' / 'old.log', 1)
    log_cleanup.cleanup_session_logs(logs, 86400)
    assert not old.exists()
    assert all(p.exists() for p in (boundary, fresh, hidden, outside, nested, logs / 'link.log'))
    log_cleanup.cleanup_session_logs(logs, 0)
    assert not fresh.exists()


def test_missing_directory_and_delete_failure(tmp_path, monkeypatch, caplog):
    log_cleanup.cleanup_session_logs(tmp_path / 'missing', 86400)
    blocked = aged_file(tmp_path / 'blocked.log', 1)
    removable = aged_file(tmp_path / 'remove.log', 1)
    unlink = Path.unlink

    def fail_one(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError('locked')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_one)
    log_cleanup.cleanup_session_logs(tmp_path, 86400)
    assert blocked.exists() and not removable.exists()
    assert 'Could not remove session log' in caplog.text


def test_startup_uses_saved_retention_and_legacy_default(monkeypatch):
    assert Config.get('tasks.logs_max_age') == 86400
    settings_store.save_values('tasks', {'base_dir': '/tasks'})
    assert Config.get('tasks.logs_max_age') == 86400
    settings_store.save_values('tasks', {'logs_max_age': 60})
    monkeypatch.setattr(log_cleanup.time, 'time', lambda: 100000)
    logs = main.DATA_DIR / 'logs'
    logs.mkdir()
    old = aged_file(logs / 'old.log', 99939)
    fresh = aged_file(logs / 'fresh.log', 99940)
    with TestClient(main.app) as client:
        assert client.get('/health').status_code == 200
        assert not old.exists()
        assert fresh.exists()


@pytest.mark.parametrize('value', [-1, 1.5, True, [], 'invalid', ''])
def test_retention_rejects_invalid_values(value):
    with TestClient(main.app) as client:
        response = client.post('/api/settings/tasks', json={'values': {'base_dir': '/tasks', 'logs_max_age': value}})
    assert response.status_code == 422


@pytest.mark.parametrize('value', [0, 60, '86400'])
def test_retention_saved_as_integer(value):
    with TestClient(main.app) as client:
        response = client.post('/api/settings/tasks', json={'values': {'base_dir': '/tasks', 'logs_max_age': value}})
    assert response.status_code == 200
    assert Config.get('tasks.logs_max_age') == int(value)
