from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

import agent_store
import board_store
import main


def counts():
    with closing(board_store.connect()) as db:
        return {row['name']: db.execute('SELECT COUNT(*) FROM "' + row['name'].replace('"', '""') + '"').fetchone()[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def test_clear_all_tables_and_reuse_schema():
    with TestClient(main.app) as client:
        board_store.create_task('DB-1', 'Task', 'Description', [])
        board_store.move_task('DB-1', 'OPEN', 0)
        run = agent_store.claim(60)
        agent_store.finish(run['id'], 'test failure')
        board_store.create_task('DB-2', 'Archived', 'Description', [])
        board_store.move_task('DB-2', 'ARCHIVE', 0)
        with closing(board_store.connect()) as db, db:
            db.execute('CREATE TABLE extra_data (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO extra_data VALUES (1, 'data')")
            db.execute("INSERT INTO board_attachments(task_pk, stored_name, original_name, size, created_at) SELECT id, 'file', 'file.txt', 1, 'now' FROM board_tasks LIMIT 1")
        board_store.FILES_DIR.joinpath('file').write_text('x')
        board_store.FILES_DIR.joinpath('orphan').write_text('old attachment')
        board_store.FILES_DIR.joinpath('.gitignore').write_text('service')
        assert client.post('/api/database/clear', json={'confirmed': True}).json() == {'cleared': True}
        assert all(value == 0 for value in counts().values())
        assert not board_store.FILES_DIR.joinpath('file').exists()
        assert not board_store.FILES_DIR.joinpath('orphan').exists()
        assert board_store.FILES_DIR.joinpath('.gitignore').read_text() == 'service'
        assert client.get('/api/board/tasks').json()['tasks'] == []
        board_store.create_task('DB-1', 'New', 'Description', [])
        assert board_store.get_task('DB-1')['title'] == 'New'
        assert client.post('/api/database/clear', json={'confirmed': True}).status_code == 200


def test_clear_requires_confirmation():
    with TestClient(main.app) as client:
        before = counts()
        for payload in ({}, {'confirmed': False}):
            assert client.post('/api/database/clear', json=payload).status_code == 422
        assert counts() == before


def test_active_run_prevents_clear():
    with TestClient(main.app) as client:
        board_store.create_task('DB-1', 'Task', 'Description', [])
        board_store.move_task('DB-1', 'OPEN', 0)
        agent_store.claim(60)
        before = counts()
        assert client.post('/api/database/clear', json={'confirmed': True}).status_code == 409
        assert counts() == before


def test_clear_rolls_back_on_failure():
    with TestClient(main.app, raise_server_exceptions=False) as client:
        board_store.create_task('DB-1', 'Task', 'Description', [])
        board_store.FILES_DIR.joinpath('file').write_text('attachment')
        with closing(board_store.connect()) as db, db:
            db.execute("CREATE TRIGGER refuse_clear BEFORE DELETE ON board_tasks BEGIN SELECT RAISE(ABORT, 'test'); END")
        before = counts()
        assert client.post('/api/database/clear', json={'confirmed': True}).status_code == 500
        assert counts() == before
        assert board_store.FILES_DIR.joinpath('file').read_text() == 'attachment'


def test_file_failure_restores_staged_files_and_database(monkeypatch):
    with TestClient(main.app, raise_server_exceptions=False) as client:
        board_store.create_task('DB-1', 'Task', 'Description', [])
        for name in ('one', 'two'):
            board_store.FILES_DIR.joinpath(name).write_text(name)
        before = counts()
        original = Path.replace
        calls = 0

        def fail_second_move(source, target):
            nonlocal calls
            if source.parent == board_store.FILES_DIR:
                calls += 1
                if calls == 2:
                    raise PermissionError('test attachment move failure')
            return original(source, target)

        monkeypatch.setattr(Path, 'replace', fail_second_move)
        assert client.post('/api/database/clear', json={'confirmed': True}).status_code == 500
        assert counts() == before
        for name in ('one', 'two'):
            assert board_store.FILES_DIR.joinpath(name).read_text() == name
        assert not list(board_store.FILES_DIR.glob('.clear-db-*'))
