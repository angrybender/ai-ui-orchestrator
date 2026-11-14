"""Persistent chat history across explicit background verification prompts."""
from contextlib import closing

import pytest

import agent_store
import board_store
from task_cycle import TaskCycle


@pytest.fixture
def running():
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('TURN-1', 'Chat turns', 'Keep each prompt response', [])
    board_store.move_task('TURN-1', 'OPEN', 0)
    run = agent_store.claim(300)
    return run['id'], TaskCycle(run, {})


def messages():
    return board_store.get_chat('TURN-1')['messages']


def test_streaming_preserves_previous_prompt_ids_text_and_dates(running):
    run_id, cycle = running
    agent_store.message(run_id, 'First partial')
    agent_store.message(run_id, 'First complete')
    with closing(board_store.connect()) as db, db:
        db.execute("UPDATE task_chat_messages SET updated_at = '2020-01-02 03:04'")
    first = messages()[0]
    cycle.next_message()
    agent_store.message(run_id, '')
    assert messages() == [first]  # No placeholder or timestamp change.
    agent_store.message(run_id, 'Second partial')
    second_id = messages()[1]['id']
    agent_store.message(run_id, 'Second complete')
    assert messages()[1]['id'] == second_id
    assert messages()[0] == first
    cycle.next_message()
    agent_store.message(run_id, 'Second complete')  # Equal text is still a new turn.
    agent_store.finish(run_id, stop_reason='end_turn')
    result = messages()
    assert [m['text'] for m in result] == ['First complete', 'Second complete', 'Second complete']
    assert len({m['id'] for m in result}) == 3
    assert {m['run_id'] for m in result} == {run_id}
    assert result[0] == first
    board_store.init_database()
    agent_store.init_database()
    assert messages() == result
    assert board_store.get_task('TURN-1')['status'] == 'REVIEW'


@pytest.mark.parametrize('answer', [None, 'Verification partial'])
def test_verification_error_does_not_modify_original_response(running, answer):
    run_id, cycle = running
    agent_store.message(run_id, 'Original answer')
    first = messages()[0]
    cycle.next_message()
    if answer:
        agent_store.message(run_id, answer)
    agent_store.finish(run_id, 'Verification failed')
    assert messages()[0] == first
    assert [m['text'] for m in messages()] == [
        'Original answer', (answer + '\n\n' if answer else '') + 'Verification failed']
    assert board_store.get_task('TURN-1')['status'] == 'WAIT'


def test_cancelled_turn_cannot_update_history(running):
    run_id, cycle = running
    agent_store.message(run_id, 'Original answer')
    cycle.next_message()
    agent_store.message(run_id, 'Verification answer')
    before = messages()
    board_store.move_task('TURN-1', 'BACKLOG', 0)
    with pytest.raises(ValueError, match='cancelled'):
        cycle.next_message()
    agent_store.message(run_id, 'Late answer')
    assert messages() == before
    agent_store.finish(run_id, 'Remote agent cancelled.', 'cancelled')
    assert messages()[:2] == before
    assert messages()[2]['role'] == 'system'


@pytest.mark.parametrize('system_role', [False, True])
@pytest.mark.parametrize('has_dates', [False, True])
def test_legacy_chat_migration_preserves_history_and_is_idempotent(system_role, has_dates):
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('TURN-1', 'Legacy chat', 'Migrate history', [])
    with closing(board_store.connect()) as db, db:
        task_pk = db.execute('SELECT id FROM board_tasks').fetchone()['id']
        db.execute('DROP TABLE task_chat_messages')
        roles = "'agent', 'user', 'system'" if system_role else "'agent', 'user'"
        dates = ', updated_at TEXT NOT NULL' if has_dates else ''
        db.execute(f"""CREATE TABLE task_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_pk INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ({roles})), text TEXT NOT NULL,
            run_id TEXT{dates}, UNIQUE(task_pk, run_id, role),
            FOREIGN KEY(task_pk) REFERENCES board_tasks(id) ON DELETE CASCADE)""")
        db.execute('CREATE INDEX task_chat_order ON task_chat_messages(task_pk, id)')
        role_values = ['agent', 'user'] + (['system'] if system_role else [])
        for number, role in enumerate(role_values, 41):
            columns = ', updated_at' if has_dates else ''
            value = ", '2020-01-02 03:04'" if has_dates else ''
            db.execute(f'INSERT INTO task_chat_messages(id, task_pk, role, text, run_id{columns}) '
                       f'VALUES (?, ?, ?, ?, ?{value})',
                       (number, task_pk, role, role + ' history', 'old-run' if role != 'user' else None))
        db.execute('ALTER TABLE agent_runs DROP COLUMN message_turn')
    board_store.init_database()
    agent_store.init_database()
    migrated = messages()
    assert [(m['id'], m['role'], m['text']) for m in migrated] == [
        (i, role, role + ' history') for i, role in enumerate(role_values, 41)]
    if has_dates:
        assert all(m['updated_at'] == '2020-01-02 03:04' for m in migrated)
    board_store.init_database()
    agent_store.init_database()
    assert messages() == migrated
    board_store.upsert_agent_message(task_pk, 'old-run', 'Verification', message_turn=1)
    assert messages()[:-1] == migrated
    assert messages()[-1]['id'] > migrated[-1]['id']
    with closing(board_store.connect()) as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
