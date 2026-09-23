from fastapi.testclient import TestClient

import agent_launcher
import agent_store
import board_store
import main


def test_start_endpoint_launches_worker_only(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_launcher, 'launch', lambda: calls.append(True) or 1234)
    with TestClient(main.app) as client:
        response = client.post('/api/agent/run')
        assert response.status_code == 202
        assert response.json() == {'pid': 1234}
    assert calls == [True]
    assert agent_store.active_run() is None
    assert board_store.list_tasks() == []


def test_start_endpoint_rejects_active_run(monkeypatch):
    monkeypatch.setattr(agent_launcher, 'launch', lambda: None)
    with TestClient(main.app) as client:
        assert client.post('/api/agent/run').status_code == 409


def test_start_endpoint_hides_spawn_error(monkeypatch):
    def fail():
        raise RuntimeError('secret')

    monkeypatch.setattr(agent_launcher, 'launch', fail)
    with TestClient(main.app) as client:
        response = client.post('/api/agent/run')
        assert response.status_code == 503
        assert 'secret' not in response.text


def test_chat_preserves_last_response_of_each_run():
    with TestClient(main.app) as client:
        board_store.create_task('CHAT-1', 'Chat', 'Details', [])
        board_store.create_task('CHAT-2', 'Other', 'Details', [])
        with board_store.connect() as connection:
            task_pk = connection.execute("SELECT id FROM board_tasks WHERE task_id = 'CHAT-1'").fetchone()['id']
            other_pk = connection.execute("SELECT id FROM board_tasks WHERE task_id = 'CHAT-2'").fetchone()['id']
        board_store.upsert_agent_message(other_pk, 'other-run', 'Other task')
        board_store.upsert_agent_message(task_pk, 'first-run', 'Initial response')
        board_store.upsert_agent_message(task_pk, 'first-run', 'First final response')
        board_store.move_task('CHAT-1', 'REVIEW', 0, force=True)
        before = client.get('/api/board/tasks/CHAT-1/chat').json()['messages'][0]
        response = client.post('/api/board/tasks/CHAT-1/chat', json={'comment': 'Continue'})
        assert response.status_code == 200
        assert response.json()['messages'][0] == before
        board_store.upsert_agent_message(task_pk, 'second-run', 'Second response')
        board_store.upsert_agent_message(task_pk, 'second-run', 'Second final response')
        board_store.upsert_agent_message(task_pk, 'restart-run', 'Restart response')
        messages = client.get('/api/board/tasks/CHAT-1/chat').json()['messages']
        assert messages[0] == before
        assert [(item['role'], item['text']) for item in messages] == [
            ('agent', 'First final response'),
            ('user', 'Continue'),
            ('agent', 'Second final response'),
            ('agent', 'Restart response'),
        ]


def test_backlog_comment_waits_for_explicit_open():
    with TestClient(main.app) as client:
        board_store.create_task('CHAT-1', 'Chat', 'Details', [])
        for comment in ['First instruction', 'Latest instruction']:
            response = client.post('/api/board/tasks/CHAT-1/chat', json={'comment': comment})
            assert response.status_code == 200
            assert response.json()['status'] == 'BACKLOG'
            assert response.json()['messages'][-1]['text'] == comment
            assert agent_store.claim(60) is None
        assert client.post('/api/board/tasks/CHAT-1/chat', json={'comment': ' '}).status_code == 422
        assert client.patch('/api/board/tasks/CHAT-1/move', json={'status': 'OPEN', 'position': 0}).status_code == 200
        assert client.post('/api/board/tasks/CHAT-1/chat', json={'comment': 'Too late'}).status_code == 409
        run = agent_store.claim(60)
        assert run['comment'] == 'Latest instruction'
        assert len(board_store.get_chat('CHAT-1')['messages']) == 2
        agent_store.finish(run['id'], 'Test cleanup')
