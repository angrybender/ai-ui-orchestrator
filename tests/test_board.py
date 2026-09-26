import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import board_store
import agent_store
import main


@pytest.mark.parametrize("status", board_store.STATUSES)
def test_archive_transition_from_board_statuses(status):
    with TestClient(main.app) as client:
        board_store.create_task("PRJ-ARCHIVE", "Archive task", "Details", [])
        with board_store.connect() as connection:
            connection.execute("UPDATE board_tasks SET status = ? WHERE task_id = ?", (status, "PRJ-ARCHIVE"))
        response = client.patch("/api/board/tasks/PRJ-ARCHIVE/move", json={"status": "ARCHIVE", "position": 0})
        if status in ("BACKLOG", "WAIT", "DONE"):
            assert response.status_code == 200
            assert response.json()["tasks"] == []
            archived = client.get("/api/archive/tasks").json()["tasks"]
            assert [task["task_id"] for task in archived] == ["PRJ-ARCHIVE"]
            assert archived[0]["status"] == "ARCHIVE"
        else:
            assert response.status_code == 422
            assert board_store.get_task("PRJ-ARCHIVE")["status"] == status


def test_multipart_creation_works_after_database_migration(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    with TestClient(main.app) as client:
        created = client.post(
            "/api/board/tasks",
            data={"task_id": "PRJ-UPLOAD", "title": "Upload", "description": "Details"},
            files={"files": ("check.txt", b"contents", "text/plain")},
        )
    assert created.status_code == 201
    assert created.json()["attachments"][0]["original_name"] == "check.txt"


def test_archive_delete_removes_task_and_attachments(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    task = board_store.create_task("PRJ-ARCHIVE", "Archived", "Details", [("file.txt", "text/plain", b"contents")])
    attachment = task["attachments"][0]
    board_store.move_task("PRJ-ARCHIVE", "ARCHIVE", 0)
    stored_path = files / board_store.connect().execute("SELECT stored_name FROM board_attachments WHERE id = ?", (attachment["id"],)).fetchone()[0]
    def delete_remote(task_id, config):
        assert board_store.get_task(task_id)["status"] == "ARCHIVE"
        assert stored_path.exists()

    monkeypatch.setattr(board_store, "delete_task_directory", delete_remote)
    with TestClient(main.app) as client:
        deleted = client.delete("/api/archive/tasks/PRJ-ARCHIVE")
        assert deleted.status_code == 204
        assert client.get("/api/archive/tasks/PRJ-ARCHIVE").status_code == 404
    assert not stored_path.exists()


def test_archive_api_paginates_archived_tasks_and_excludes_board_tasks(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    board_store.create_task("PRJ-1", "First", "Details", [])
    board_store.create_task("PRJ-2", "Second", "Details", [])
    board_store.move_task("PRJ-1", "ARCHIVE", 0)
    with TestClient(main.app) as client:
        archived = client.get("/api/archive/tasks?page=1")
        assert archived.status_code == 200
        assert [task["task_id"] for task in archived.json()["tasks"]] == ["PRJ-1"]
        assert archived.json()["page_size"] == 20
        assert client.get("/api/board/tasks").json()["tasks"][0]["task_id"] == "PRJ-2"
        assert client.get("/archive").status_code == 200


def test_board_api_create_and_allowed_transition(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    with TestClient(main.app) as client:
        created = client.post(
            "/api/board/tasks",
            json={"task_id": "PRJ-1", "title": "First task", "description": "Details"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "BACKLOG"
        assert created.json()["created_at"]
        moved = client.patch("/api/board/tasks/PRJ-1/move", json={"status": "OPEN", "position": 0})
        assert moved.status_code == 200
        assert moved.json()["tasks"][0]["status"] == "OPEN"


@pytest.mark.parametrize("method", ["move", "save", "multipart"])
def test_open_can_return_to_backlog_without_agent_claim(method):
    with TestClient(main.app) as client:
        original = board_store.create_task("RETURN-1", "Queued task", "Details", [("note.txt", "text/plain", b"note")])
        board_store.move_task("RETURN-1", "OPEN", 0)
        board_store.create_task("RETURN-2", "Existing backlog task", "Details", [])
        if method == "move":
            response = client.patch("/api/board/tasks/RETURN-1/move", json={"status": "BACKLOG", "position": 0})
        else:
            payload = {"title": "Edited task", "description": "Edited details", "status": "BACKLOG"}
            if method == "multipart":
                response = client.put("/api/board/tasks/RETURN-1", data=payload,
                                      files={"files": ("new.txt", b"new", "text/plain")})
            else:
                response = client.put("/api/board/tasks/RETURN-1", json=payload)
        assert response.status_code == 200
        task = client.get("/api/board/tasks/RETURN-1").json()
        assert task["status"] == "BACKLOG"
        assert task["title"] == ("Queued task" if method == "move" else "Edited task")
        assert task["description"] == ("Details" if method == "move" else "Edited details")
        assert task["attachments"][0] == original["attachments"][0]
        backlog = [item["task_id"] for item in board_store.list_tasks() if item["status"] == "BACKLOG"]
        assert backlog == (["RETURN-1", "RETURN-2"] if method == "move" else ["RETURN-2", "RETURN-1"])
        assert agent_store.claim(300) is None
        board_store.move_task("RETURN-1", "OPEN", 0)
        assert agent_store.claim(300)["task_id"] == "RETURN-1"


def test_board_api_create_and_update_multiple_attachments(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    with TestClient(main.app) as client:
        created = client.post(
            "/api/board/tasks",
            data={"task_id": "PRJ-1", "title": "First task", "description": "Details"},
            files=[
                ("files", ("first.txt", b"first", "text/plain")),
                ("files", ("second.txt", b"second", "text/plain")),
            ],
        )
        assert created.status_code == 201
        assert [item["original_name"] for item in created.json()["attachments"]] == ["first.txt", "second.txt"]

        updated = client.put(
            "/api/board/tasks/PRJ-1",
            data={"title": "Updated task", "description": "Updated details", "status": "BACKLOG"},
            files=[("files", ("third.txt", b"third", "text/plain"))],
        )
        assert updated.status_code == 200
        assert [item["original_name"] for item in updated.json()["attachments"]] == [
            "first.txt",
            "second.txt",
            "third.txt",
        ]


def test_board_api_rejects_invalid_transition_and_validation(tmp_path, monkeypatch):
    database = tmp_path / "board.db"
    files = tmp_path / "files"
    monkeypatch.setattr(board_store, "DATABASE_PATH", database)
    monkeypatch.setattr(board_store, "FILES_DIR", files)
    board_store.init_database()
    with TestClient(main.app) as client:
        invalid = client.post("/api/board/tasks", json={"task_id": "PRJ-1", "title": "", "description": ""})
        assert invalid.status_code == 422
        assert "title" in invalid.json()["detail"]
        client.post("/api/board/tasks", json={"task_id": "PRJ-1", "title": "First", "description": "Details"})
        forbidden = client.patch("/api/board/tasks/PRJ-1/move", json={"status": "DONE", "position": 0})
        assert forbidden.status_code == 422


def test_update_cleanup_failure_preserves_committed_upload(monkeypatch):
    board_store.init_database()
    task = board_store.create_task("FILES-1", "Title", "Details", [("old.txt", None, b"old")])
    old_id = task["attachments"][0]["id"]
    old_path, _, _ = board_store.attachment_path("FILES-1", old_id)
    original_unlink = Path.unlink

    def fail_old_unlink(path, *args, **kwargs):
        if path == old_path:
            raise PermissionError("Cannot remove old file")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    with pytest.raises(PermissionError):
        board_store.update_task("FILES-1", "Updated", "Details", "BACKLOG", [old_id],
                                [("new.txt", None, b"new")])
    saved = board_store.get_task("FILES-1")
    assert saved["title"] == "Updated"
    assert [item["original_name"] for item in saved["attachments"]] == ["new.txt"]
    new_path, _, _ = board_store.attachment_path("FILES-1", saved["attachments"][0]["id"])
    assert new_path.read_bytes() == b"new"


@pytest.mark.parametrize("operation", ["create", "update"])
def test_response_read_failure_preserves_committed_upload(monkeypatch, operation):
    board_store.init_database()
    if operation == "update":
        board_store.create_task("FILES-1", "Title", "Details", [])
    original_get = board_store.get_task

    def fail_get(task_id):
        raise sqlite3.OperationalError("Response read failed")

    monkeypatch.setattr(board_store, "get_task", fail_get)
    with pytest.raises(sqlite3.OperationalError, match="Response read failed"):
        if operation == "create":
            board_store.create_task("FILES-1", "Title", "Details", [("new.txt", None, b"new")])
        else:
            board_store.update_task("FILES-1", "Title", "Details", "BACKLOG", [],
                                    [("new.txt", None, b"new")])
    saved = original_get("FILES-1")
    assert len(saved["attachments"]) == 1
    path, _, _ = board_store.attachment_path("FILES-1", saved["attachments"][0]["id"])
    assert path.read_bytes() == b"new"


@pytest.mark.parametrize("operation", ["create", "update", "move"])
def test_mutations_lock_before_reading_order_or_status(monkeypatch, operation):
    board_store.init_database()
    board_store.create_task("RACE-1", "Title", "Details", [])
    board_store.move_task("RACE-1", "OPEN", 0)
    original_connect = board_store.connect
    attempts = []

    def connect_with_competing_writer():
        connection = original_connect()

        def before_select(sql):
            # Only the first read of the mutation is relevant; result serialization
            # legitimately runs after its transaction has committed.
            if attempts or not sql.startswith("SELECT"):
                return
            attempts.append(sql)
            competitor = sqlite3.connect(board_store.DATABASE_PATH, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("UPDATE board_tasks SET status = 'IN PROGRESS' WHERE task_id = 'RACE-1'")
            finally:
                competitor.close()

        # Trace callback exceptions are swallowed by sqlite3, so record the
        # attempted write's result outside the callback as well.
        def trace(sql):
            try:
                before_select(sql)
            except BaseException as error:
                failures.append(error)

        connection.set_trace_callback(trace)
        return connection

    failures = []
    monkeypatch.setattr(board_store, "connect", connect_with_competing_writer)
    if operation == "create":
        board_store.create_task("RACE-2", "Title", "Details", [])
    elif operation == "update":
        board_store.update_task("RACE-1", "Edited", "Details", "OPEN", [], [])
    else:
        board_store.move_task("RACE-1", "OPEN", 0)
    assert attempts
    assert not failures, failures
    assert board_store.get_task("RACE-1")["status"] == "OPEN"
