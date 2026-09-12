import json

import board_store
import move_task


def _setup_board(tmp_path, monkeypatch):
    monkeypatch.setattr(board_store, "DATABASE_PATH", tmp_path / "board.db")
    monkeypatch.setattr(board_store, "FILES_DIR", tmp_path / "files")
    board_store.init_database()
    board_store.create_task("PRJ-1", "First task", "Details", [])


def test_cli_moves_task_and_prints_updated_task(tmp_path, monkeypatch, capsys):
    _setup_board(tmp_path, monkeypatch)

    result = move_task.main(["PRJ-1", "OPEN"])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["status"] == "OPEN"
    assert captured.err == ""
    assert board_store.get_task("PRJ-1")["status"] == "OPEN"


def test_cli_moves_task_to_error_in_open_column(tmp_path, monkeypatch, capsys):
    _setup_board(tmp_path, monkeypatch)

    result = move_task.main(["PRJ-1", "ERROR"])

    captured = capsys.readouterr()
    assert result == 0
    task = json.loads(captured.out)
    assert task["status"] == "WAIT"
    assert task["is_error"] is True
    assert captured.err == ""
    assert board_store.get_task("PRJ-1")["status"] == "WAIT"
    assert board_store.get_task("PRJ-1")["is_error"] is True


def test_cli_reports_missing_task(tmp_path, monkeypatch, capsys):
    _setup_board(tmp_path, monkeypatch)

    result = move_task.main(["PRJ-404", "OPEN"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "Error: Task not found\n"


def test_store_rejects_forbidden_transition_without_force(tmp_path, monkeypatch):
    _setup_board(tmp_path, monkeypatch)

    try:
        board_store.move_task("PRJ-1", "DONE", 0)
    except board_store.BoardError as error:
        assert "Transition from BACKLOG to DONE is not allowed" in str(error)
    else:
        raise AssertionError("Expected forbidden transition to fail")
