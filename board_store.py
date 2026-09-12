from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from settings.config import Config
from task_remote import delete_task_directory

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "data" / "app.db"
FILES_DIR = BASE_DIR / "data" / "files"
STATUSES = ("BACKLOG", "OPEN", "WAIT", "IN PROGRESS", "REVIEW", "DONE")
ARCHIVE_STATUS = "ARCHIVE"
ALL_STATUSES = (*STATUSES, ARCHIVE_STATUS)
ALLOWED_TRANSITIONS = {
    "BACKLOG": {"OPEN", ARCHIVE_STATUS},
    "IN PROGRESS": {"BACKLOG"},
    "REVIEW": {"OPEN", "BACKLOG", "DONE"},
    "DONE": {ARCHIVE_STATUS},
}


class BoardError(ValueError):
    pass


class TaskNotFoundError(BoardError):
    pass


class TaskConflictError(BoardError):
    pass


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS board_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE', 'ARCHIVE')),
                sort_order INTEGER NOT NULL,
                is_error INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS board_tasks_status_order
                ON board_tasks(status, sort_order);
            CREATE TABLE IF NOT EXISTS task_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_pk INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('agent', 'user')),
                text TEXT NOT NULL,
                run_id TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(task_pk, run_id, role),
                FOREIGN KEY(task_pk) REFERENCES board_tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS task_chat_order ON task_chat_messages(task_pk, id);
            CREATE TABLE IF NOT EXISTS board_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_pk INTEGER NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                content_type TEXT,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_pk) REFERENCES board_tasks(id) ON DELETE CASCADE
            );
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(board_tasks)").fetchall()}
        chat_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_chat_messages)").fetchall()}
        if "updated_at" not in chat_columns:
            connection.execute("ALTER TABLE task_chat_messages ADD COLUMN updated_at TEXT")
            connection.execute("UPDATE task_chat_messages SET updated_at = ? WHERE updated_at IS NULL", (datetime.now().strftime("%Y-%m-%d %H:%M"),))
        if "is_error" not in columns:
            connection.execute("ALTER TABLE board_tasks ADD COLUMN is_error INTEGER NOT NULL DEFAULT 0")
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'board_tasks'"
        ).fetchone()[0]
        if "'ARCHIVE'" not in table_sql:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("ALTER TABLE board_tasks RENAME TO board_tasks_old")
            connection.execute(
                """
                CREATE TABLE board_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE', 'ARCHIVE')),
                    sort_order INTEGER NOT NULL,
                    is_error INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            old_columns = {row["name"] for row in connection.execute("PRAGMA table_info(board_tasks_old)").fetchall()}
            error_column = "is_error" if "is_error" in old_columns else "0"
            connection.execute(
                f"INSERT INTO board_tasks(id, task_id, title, description, status, sort_order, is_error, created_at) "
                f"SELECT id, task_id, title, description, status, sort_order, {error_column}, created_at FROM board_tasks_old"
            )
            connection.execute("DROP TABLE board_tasks_old")
            connection.execute("PRAGMA foreign_keys = ON")
        attachment_fk = connection.execute("PRAGMA foreign_key_list(board_attachments)").fetchall()
        if attachment_fk and attachment_fk[0][2] != "board_tasks":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("ALTER TABLE board_attachments RENAME TO board_attachments_old")
            connection.execute(
                """
                CREATE TABLE board_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_pk INTEGER NOT NULL,
                    stored_name TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    content_type TEXT,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_pk) REFERENCES board_tasks(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "INSERT INTO board_attachments(id, task_pk, stored_name, original_name, content_type, size, created_at) "
                "SELECT id, task_pk, stored_name, original_name, content_type, size, created_at FROM board_attachments_old"
            )
            connection.execute("DROP TABLE board_attachments_old")
            connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE INDEX IF NOT EXISTS board_tasks_status_order ON board_tasks(status, sort_order)")


def _attachments(connection: sqlite3.Connection, task_pk: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, original_name, content_type, size FROM board_attachments WHERE task_pk = ? ORDER BY id",
        (task_pk,),
    ).fetchall()
    return [dict(row) for row in rows]


def _serialize(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    task["is_error"] = bool(task["is_error"])
    task["attachments"] = _attachments(connection, task.pop("id"))
    return task


def list_tasks() -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM board_tasks WHERE status != ? ORDER BY CASE status "
            + " ".join(f"WHEN '{status}' THEN {index}" for index, status in enumerate(STATUSES))
            + " END, sort_order, id",
            (ARCHIVE_STATUS,),
        ).fetchall()
        return [_serialize(connection, row) for row in rows]


def get_task(task_id: str) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute("SELECT * FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError("Task not found")
        return _serialize(connection, row)


def list_archive(page: int, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    if page < 1 or page_size < 1:
        raise BoardError("Invalid page")
    with connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM board_tasks WHERE status = ?", (ARCHIVE_STATUS,)).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM board_tasks WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (ARCHIVE_STATUS, page_size, (page - 1) * page_size),
        ).fetchall()
        return [_serialize(connection, row) for row in rows], total


def delete_archived_task(task_id: str) -> None:
    _delete_task(task_id, ARCHIVE_STATUS)


def delete_backlog_task(task_id: str) -> None:
    _delete_task(task_id, "BACKLOG")


def _delete_task(task_id: str, required_status: str) -> None:
    paths: list[Path] = []
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT id, status FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != required_status:
            raise TaskNotFoundError("Task not found")
        if required_status == "BACKLOG":
            delete_task_directory(task_id, Config, allow_connection_unavailable=True)
        else:
            delete_task_directory(task_id, Config)
        attachments = connection.execute(
            "SELECT stored_name FROM board_attachments WHERE task_pk = ?", (row["id"],)
        ).fetchall()
        paths = [FILES_DIR / attachment["stored_name"] for attachment in attachments]
        connection.execute("DELETE FROM board_tasks WHERE id = ?", (row["id"],))
    for path in paths:
        path.unlink(missing_ok=True)


def get_chat(task_id: str) -> dict[str, Any]:
    with connect() as connection:
        task = connection.execute("SELECT id, status FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            raise TaskNotFoundError("Task not found")
        rows = connection.execute(
            "SELECT id, role, text, updated_at FROM task_chat_messages "
            "WHERE task_pk = ? ORDER BY id",
            (task["id"],),
        ).fetchall()
        return {"status": task["status"], "messages": [dict(row) for row in rows]}


def add_user_comment(task_id: str, comment: str) -> dict[str, Any]:
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 32000:
        raise BoardError("Invalid comment")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute("SELECT id, status FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            raise TaskNotFoundError("Task not found")
        if task["status"] not in ("REVIEW", "WAIT"):
            raise TaskConflictError("Comments are allowed only in REVIEW or WAIT")
        connection.execute("INSERT INTO task_chat_messages(task_pk, role, text, updated_at) VALUES (?, 'user', ?, ?)", (task["id"], comment.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")))
        connection.execute("UPDATE board_tasks SET status = 'OPEN' WHERE id = ?", (task["id"],))
    return get_chat(task_id)


def upsert_agent_message(task_pk: int, run_id: str, text: str) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO task_chat_messages(task_pk, role, text, run_id, updated_at) VALUES (?, 'agent', ?, ?, ?) ON CONFLICT(task_pk, run_id, role) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at", (task_pk, text, run_id, datetime.now().strftime("%Y-%m-%d %H:%M")))


def next_task_id() -> str:
    prefix = str(Config.get("board.task_prefix") or "TASK").strip() or "TASK"
    with connect() as connection:
        rows = connection.execute("SELECT task_id FROM board_tasks ORDER BY id DESC").fetchall()
        number = 1
        if rows:
            _, separator, suffix = rows[0]["task_id"].rpartition("-")
            if separator and suffix.isdigit():
                number = int(suffix) + 1
        while connection.execute("SELECT 1 FROM board_tasks WHERE task_id = ?", (f"{prefix}-{number}",)).fetchone():
            number += 1
        return f"{prefix}-{number}"


def _validate(task_id: str, title: str, description: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not task_id.strip():
        errors["task_id"] = "Task ID is required"
    if not title.strip():
        errors["title"] = "Title is required"
    elif len(title) > 1000:
        errors["title"] = "Title must be 1000 characters or fewer"
    if not description.strip():
        errors["description"] = "Description is required"
    elif len(description) > 32000:
        errors["description"] = "Description must be 32000 characters or fewer"
    return errors


def create_task(task_id: str, title: str, description: str, files: list[tuple[str, str | None, bytes]]) -> dict[str, Any]:
    errors = _validate(task_id, title, description)
    if errors:
        raise BoardError(errors)
    written: list[Path] = []
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM board_tasks WHERE status = 'BACKLOG'"
            ).fetchone()[0]
            try:
                cursor = connection.execute(
                    "INSERT INTO board_tasks(task_id, title, description, status, sort_order, created_at) VALUES (?, ?, ?, 'BACKLOG', ?, ?)",
                    (task_id.strip(), title.strip(), description.strip(), order, datetime.now().strftime("%Y-%m-%d %H:%M")),
                )
            except sqlite3.IntegrityError as error:
                raise TaskConflictError({"task_id": "Task ID already exists"}) from error
            _save_files(connection, cursor.lastrowid, files, written)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return get_task(task_id.strip())


def _save_files(
    connection: sqlite3.Connection,
    task_pk: int,
    files: list[tuple[str, str | None, bytes]],
    written: list[Path],
) -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    for original_name, content_type, content in files:
        stored_name = uuid4().hex
        path = FILES_DIR / stored_name
        path.write_bytes(content)
        written.append(path)
        connection.execute(
            "INSERT INTO board_attachments(task_pk, stored_name, original_name, content_type, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_pk, stored_name, Path(original_name).name, content_type, len(content), datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def update_task(
    task_id: str,
    title: str,
    description: str,
    status: str,
    remove_attachment_ids: list[int],
    files: list[tuple[str, str | None, bytes]],
) -> dict[str, Any]:
    errors = _validate(task_id, title, description)
    if errors:
        raise BoardError(errors)
    written: list[Path] = []
    removed_paths: list[Path] = []
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError("Task not found")
            if status not in ALL_STATUSES:
                raise BoardError({"status": "Unknown status"})
            if status != row["status"] and status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
                raise BoardError({"status": f"Transition from {row['status']} to {status} is not allowed"})
            order = row["sort_order"]
            if status != row["status"]:
                order = connection.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM board_tasks WHERE status = ?", (status,)
                ).fetchone()[0]
            connection.execute(
                "UPDATE board_tasks SET title = ?, description = ?, status = ?, sort_order = ? WHERE id = ?",
                (title.strip(), description.strip(), status, order, row["id"]),
            )
            for attachment_id in remove_attachment_ids:
                attachment = connection.execute(
                    "SELECT stored_name FROM board_attachments WHERE id = ? AND task_pk = ?",
                    (attachment_id, row["id"]),
                ).fetchone()
                if attachment:
                    removed_paths.append(FILES_DIR / attachment["stored_name"])
                    connection.execute("DELETE FROM board_attachments WHERE id = ?", (attachment_id,))
            _save_files(connection, row["id"], files, written)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for path in removed_paths:
        path.unlink(missing_ok=True)
    return get_task(task_id)


def move_task(
    task_id: str,
    status: str,
    position: int,
    force: bool = False,
    force_error: bool | None = None,
) -> list[dict[str, Any]]:
    if status not in ALL_STATUSES or position < 0:
        raise BoardError("Invalid move")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM board_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError("Task not found")
        source_status = row["status"]
        if not force and status != source_status and status not in ALLOWED_TRANSITIONS.get(source_status, set()):
            raise BoardError(f"Transition from {source_status} to {status} is not allowed")
        destination_ids = [
            item["id"]
            for item in connection.execute(
                "SELECT id FROM board_tasks WHERE status = ? AND id != ? ORDER BY sort_order, id",
                (status, row["id"]),
            ).fetchall()
        ]
        destination_ids.insert(min(position, len(destination_ids)), row["id"])
        error_value = row["is_error"] if force_error is None else force_error
        connection.execute("UPDATE board_tasks SET status = ?, is_error = ? WHERE id = ?", (status, error_value, row["id"]))
        for index, task_pk in enumerate(destination_ids):
            connection.execute("UPDATE board_tasks SET sort_order = ? WHERE id = ?", (index, task_pk))
        if source_status != status:
            source_ids = connection.execute(
                "SELECT id FROM board_tasks WHERE status = ? ORDER BY sort_order, id", (source_status,)
            ).fetchall()
            for index, source in enumerate(source_ids):
                connection.execute("UPDATE board_tasks SET sort_order = ? WHERE id = ?", (index, source["id"]))
    return list_tasks()


def attachment_path(task_id: str, attachment_id: int) -> tuple[Path, str, str | None]:
    with connect() as connection:
        row = connection.execute(
            "SELECT a.stored_name, a.original_name, a.content_type FROM board_attachments a "
            "JOIN board_tasks t ON t.id = a.task_pk WHERE t.task_id = ? AND a.id = ?",
            (task_id, attachment_id),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError("Attachment not found")
        return FILES_DIR / row["stored_name"], row["original_name"], row["content_type"]
