import agent_store
import board_store


def test_finish_appends_error_to_existing_agent_response(tmp_path, monkeypatch):
    monkeypatch.setattr(board_store, 'DATABASE_PATH', tmp_path / 'board.db')
    monkeypatch.setattr(board_store, 'FILES_DIR', tmp_path / 'files')
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('CHAT-ERROR', 'Title', 'Description', [])
    board_store.move_task('CHAT-ERROR', 'OPEN', 0)
    run = agent_store.claim(60)
    agent_store.message(run['id'], 'Ответ агента до ошибки')
    agent_store.finish(run['id'], 'Remote agent execution failed.')
    chat = board_store.get_chat('CHAT-ERROR')
    assert len(chat['messages']) == 1
    assert chat['messages'][0]['text'] == 'Ответ агента до ошибки\n\nRemote agent execution failed.'
    assert chat['status'] == 'WAIT'
