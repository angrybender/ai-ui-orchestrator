import asyncio
import json

import pytest

import agent_store
import board_events
import board_store
from controllers.board import events


class Request:
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def payload(event):
    assert event.startswith("event: board\ndata: ")
    return json.loads(event.split("data: ", 1)[1])["tasks"]


@pytest.mark.parametrize("error", [None, "Execution failed"])
def test_stream_observes_claim_finish_and_reconnect(tmp_path, monkeypatch, error):
    monkeypatch.setattr(board_store, "DATABASE_PATH", tmp_path / "board.db")
    monkeypatch.setattr(board_store, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(board_events, "POLL_INTERVAL", 0)
    monkeypatch.setattr(board_events, "HEARTBEAT_POLLS", 1)
    board_store.init_database()
    agent_store.init_database()

    async def check():
        request = Request()
        stream = board_events.task_events(request)
        assert payload(await anext(stream)) == []
        board_store.create_task("LIVE-1", "Live task", "Description", [])
        assert payload(await anext(stream))[0]["status"] == "BACKLOG"
        board_store.move_task("LIVE-1", "OPEN", 0)
        assert payload(await anext(stream))[0]["status"] == "OPEN"
        run = agent_store.claim(60)
        assert payload(await anext(stream))[0]["status"] == "IN PROGRESS"
        assert await anext(stream) == ": keepalive\n\n"
        agent_store.finish(run["id"], error=error)
        task = payload(await anext(stream))[0]
        assert task["status"] == ("WAIT" if error else "REVIEW")
        assert task["is_error"] is bool(error)
        if error:
            chat = board_store.get_chat(run["task_id"])
            assert chat["messages"][-1]["role"] == "agent"
            assert chat["messages"][-1]["text"] == error
        await stream.aclose()
        stream = board_events.task_events(request)
        assert payload(await anext(stream))[0] == task
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(check())


def test_stream_headers_and_cancellation():
    async def check():
        request = Request()
        response = await events(request)
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

    asyncio.run(check())


@pytest.mark.parametrize('deleted', [False, True])
def test_stream_reports_recovery_without_visible_task_changes(monkeypatch, deleted):
    monkeypatch.setattr(board_events, 'POLL_INTERVAL', 0)
    monkeypatch.setattr(board_events, 'HEARTBEAT_POLLS', 1)
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('REC-1', 'Blocked task', 'Description', [])
    board_store.move_task('REC-1', 'OPEN', 0)
    run = agent_store.claim(60)
    agent_store.finish(run['id'], 'Remote stop unconfirmed.', agent_uncertain=True)
    board_store.move_task('REC-1', 'ARCHIVE', 0, force=True)
    if deleted:
        with board_store.connect() as connection:
            connection.execute("DELETE FROM board_tasks WHERE task_id = 'REC-1'")

    async def check():
        stream = board_events.task_events(Request())
        try:
            data = json.loads((await anext(stream)).split('data: ', 1)[1])
            assert data == {'tasks': [], 'recovery_runs': [
                {'run_id': run['id'], 'task_id': None if deleted else 'REC-1'}
            ]}
            assert await anext(stream) == ': keepalive\n\n'
            agent_store.confirm_remote_stop(run['id'])
            data = json.loads((await anext(stream)).split('data: ', 1)[1])
            assert data == {'tasks': [], 'recovery_runs': []}
        finally:
            await stream.aclose()

    asyncio.run(check())
