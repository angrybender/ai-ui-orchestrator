import errno
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

import board_store
import main
import task_remote
from agent_remote import RemoteAgentError


@pytest.fixture
def remote(monkeypatch):
    ssh = MagicMock()
    sftp = ssh.open_sftp.return_value
    monkeypatch.setattr(task_remote.paramiko, "SSHClient", lambda: ssh)
    sftp.normalize.side_effect = lambda path: path
    sftp.lstat.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR),
                             SimpleNamespace(st_mode=stat.S_IFLNK),
                             FileNotFoundError(errno.ENOENT, "missing")]
    sftp.listdir_attr.return_value = [SimpleNamespace(filename="link")]
    config = {"remote_server.host": "host:2222", "remote_server.username": "user",
              "remote_server.password": "secret", "tasks.base_dir": "/tasks"}
    return ssh, sftp, config


def test_remote_delete_and_close(remote):
    ssh, sftp, config = remote
    task_remote.delete_task_directory("TASK-1", config)
    assert ssh.connect.call_args.kwargs["port"] == 2222
    sftp.remove.assert_called_once_with("/tasks/TASK-1/link")
    sftp.rmdir.assert_called_once_with("/tasks/TASK-1")
    sftp.close.assert_called_once()
    ssh.close.assert_called_once()


def test_missing_directory_requires_connection(remote):
    ssh, sftp, config = remote
    sftp.lstat.side_effect = FileNotFoundError(errno.ENOENT, "missing")
    task_remote.delete_task_directory("TASK-1", config)
    ssh.connect.assert_called_once()
    sftp.rmdir.assert_not_called()


@pytest.mark.parametrize("failure", ["connect", "remove", "rmdir", "permission", "verify"])
def test_remote_errors_preserve_database_and_files(remote, monkeypatch, failure):
    ssh, sftp, config = remote
    monkeypatch.setattr(board_store, "Config", SimpleNamespace(get=config.get))
    if failure == "connect":
        ssh.connect.side_effect = OSError("secret")
    elif failure == "permission":
        sftp.lstat.side_effect = PermissionError(errno.EACCES, "secret")
    elif failure == "verify":
        sftp.lstat.side_effect = None
        sftp.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR)
        sftp.listdir_attr.return_value = []
    else:
        getattr(sftp, failure).side_effect = OSError("secret")
    board_store.init_database()
    task = board_store.create_task("TASK-1", "Title", "Details", [("file.txt", "text/plain", b"data")])
    board_store.move_task("TASK-1", "ARCHIVE", 0)
    with TestClient(main.app) as client:
        response = client.delete("/api/archive/tasks/TASK-1")
    assert response.status_code == 502
    assert "secret" not in response.text
    assert board_store.get_task("TASK-1")["attachments"] == task["attachments"]
    assert len(list(board_store.FILES_DIR.iterdir())) == 1
    ssh.close.assert_called_once()


@pytest.mark.parametrize("allow_connection_unavailable", [False, True])
@pytest.mark.parametrize("task_id", ["..", "../escape", "bad/name", "bad\\name"])
def test_unsafe_id_never_connects(remote, task_id, allow_connection_unavailable):
    ssh, sftp, config = remote
    with pytest.raises(RemoteAgentError):
        task_remote.delete_task_directory(task_id, config, allow_connection_unavailable=allow_connection_unavailable)
    ssh.connect.assert_not_called()


def test_root_symlink_rejected(remote):
    ssh, sftp, config = remote
    sftp.lstat.side_effect = None
    sftp.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFLNK)
    with pytest.raises(RemoteAgentError):
        task_remote.delete_task_directory("TASK-1", config)
    sftp.rmdir.assert_not_called()


def test_non_archived_and_missing_tasks_do_not_connect(remote):
    ssh, sftp, config = remote
    board_store.init_database()
    board_store.create_task("TASK-1", "Title", "Details", [])
    with TestClient(main.app) as client:
        assert client.delete("/api/archive/tasks/TASK-1").status_code == 404
        assert client.delete("/api/archive/tasks/MISSING").status_code == 404
    ssh.connect.assert_not_called()


@pytest.mark.parametrize("failure", [None, "missing", "missing_base", "offline", "timeout", "ssh"])
def test_backlog_delete_removes_local_data(remote, monkeypatch, failure):
    ssh, sftp, config = remote
    monkeypatch.setattr(board_store, "Config", SimpleNamespace(get=config.get))
    if failure == "missing_base":
        sftp.normalize.side_effect = FileNotFoundError(errno.ENOENT, "missing")
    elif failure == "missing":
        sftp.lstat.side_effect = FileNotFoundError(errno.ENOENT, "missing")
    elif failure:
        errors = {"offline": ConnectionRefusedError("secret"), "timeout": TimeoutError("secret"),
                  "ssh": task_remote.paramiko.SSHException("secret")}
        ssh.connect.side_effect = errors[failure]
    board_store.init_database()
    board_store.create_task("TASK-1", "Title", "Details", [("file.txt", "text/plain", b"data")])
    with TestClient(main.app) as client:
        assert client.delete("/api/board/tasks/TASK-1").status_code == 204
        assert client.get("/api/board/tasks/TASK-1").status_code == 404
    with board_store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM board_attachments").fetchone()[0] == 0
    assert not list(board_store.FILES_DIR.iterdir())
    ssh.close.assert_called_once()


@pytest.mark.parametrize("failure", ["lstat", "listdir_attr", "remove", "rmdir"])
def test_backlog_delete_failure_preserves_local_data(remote, monkeypatch, failure):
    ssh, sftp, config = remote
    monkeypatch.setattr(board_store, "Config", SimpleNamespace(get=config.get))
    getattr(sftp, failure).side_effect = PermissionError(errno.EACCES, "secret")
    board_store.init_database()
    task = board_store.create_task("TASK-1", "Title", "Details", [("file.txt", "text/plain", b"data")])
    with TestClient(main.app) as client:
        response = client.delete("/api/board/tasks/TASK-1")
    assert response.status_code == 502
    assert "secret" not in response.text
    assert board_store.get_task("TASK-1")["attachments"] == task["attachments"]
    assert len(list(board_store.FILES_DIR.iterdir())) == 1


@pytest.mark.parametrize("status", ["OPEN", "WAIT", "IN PROGRESS", "REVIEW", "DONE", "ARCHIVE"])
def test_backlog_endpoint_rejects_other_statuses(remote, status):
    ssh, sftp, config = remote
    board_store.init_database()
    board_store.create_task("TASK-1", "Title", "Details", [])
    with board_store.connect() as connection:
        connection.execute("UPDATE board_tasks SET status = ?", (status,))
    with TestClient(main.app) as client:
        assert client.delete("/api/board/tasks/TASK-1").status_code == 404
        assert client.delete("/api/board/tasks/MISSING").status_code == 404
    ssh.connect.assert_not_called()



def test_delete_requires_configured_base_directory(remote):
    ssh, sftp, config = remote
    config.pop('tasks.base_dir')
    with pytest.raises(RemoteAgentError):
        task_remote.delete_task_directory('TASK-1', config)
    ssh.connect.assert_not_called()
