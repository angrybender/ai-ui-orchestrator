"""Explicit SSH/model integration for Clear context with existing remote providers.

PYTHONPATH=.:tests dev-venv/bin/python tests/live_task_context.py --report /tmp/acp-context-live-results.json
"""
import argparse
from contextlib import closing
import gc
import json
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_remote
import agent_store
import agent_worker
import board_store
from controllers.board import router
from live_current_environment import CurrentEnvironment
from settings.config import Config


def trace(path):
    rows = []
    for line in path.read_text(errors='replace').splitlines():
        if line.startswith('OUT '):
            try:
                rows.append(json.loads(line[4:]))
            except ValueError:
                pass
    return rows


def model_ids(path):
    models = set()
    for line in path.read_text(errors='replace').splitlines():
        if not line.startswith('IN '):
            continue
        try:
            result = json.loads(line[3:]).get('result', {})
        except ValueError:
            continue
        if not isinstance(result, dict):
            continue
        current = (result.get('models') or {}).get('currentModelId')
        if current:
            models.add(current)
        for option in result.get('configOptions') or []:
            if option.get('category') == 'model' and isinstance(option.get('currentValue'), str):
                models.add(option['currentValue'])
    return sorted(models)


def run_agent(env, agent, storage, deadline, emit):
    config = env.runtime(agent)
    task_id = 'LIVE-CLEAR-'+agent.upper()
    cwd = config['tasks.base_dir']+'/'+task_id
    nonce, new_nonce = 'MEMORY_'+uuid4().hex, 'NEW_'+uuid4().hex
    artifact = 'PERSISTED_'+uuid4().hex
    sessions, phases = [], []
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    board_store.create_task(task_id, 'ACP context reset integration',
        'This is an isolated integration test. Execute the latest user instruction only. '
        'Work only in this task directory. Do not read external session histories or settings. '
        'Use native foreground file tools. No background work or subagents. '
        'Keep result.txt unchanged after creating it; it is a persistent test artifact. '
        'Do not save conversation-only memory tokens in files.', [])
    base = '/api/board/tasks/'+task_id

    def chat():
        response = client.get(base+'/chat')
        assert response.status_code == 200
        return response.json()

    def execute(name, comment, expected_session=None):
        before = chat()['messages']
        response = client.post(base+'/chat', json={'comment': comment})
        assert response.status_code == 200
        if response.json()['status'] == 'BACKLOG':
            assert client.patch(base+'/move', json={'status': 'OPEN', 'position': 0}).status_code == 200
        stop, observed = threading.Event(), {}
        started = time.monotonic()
        raw = storage/(agent+'-'+name+'.log')

        def monitor():
            try:
                while not stop.wait(.1):
                    current = board_store.get_task(task_id)
                    if current['status'] == 'IN PROGRESS' and not observed.get('active_checked'):
                        observed['active_checked'] = True
                        observed['reset_status'] = client.post(base+'/clear-context').status_code
                        observed['hidden_while_running'] = not chat()['can_clear_context']
                    if time.monotonic()-started > deadline:
                        observed['timeout'] = True
                        board_store.move_task(task_id, 'BACKLOG', 0)
                        return
            except Exception as error:
                observed['monitor_error'] = type(error).__name__

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        emit({'agent': agent, 'phase': name, 'event': 'started'})
        original_get = Config.get
        with patch.object(Config, 'get', side_effect=lambda key: config[key] if key in config else original_get(key)), \
                patch.object(agent_remote, '_open_raw_log', side_effect=lambda _: raw.open('ab')):
            monitor_thread.start()
            try:
                code = agent_worker.execute()
            finally:
                stop.set()
                monitor_thread.join(5)
        with closing(board_store.connect()) as db:
            row = dict(db.execute('SELECT * FROM agent_runs WHERE task_pk = '
                                 '(SELECT id FROM board_tasks WHERE task_id = ?) ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone())
        if row['session_id'] and row['session_id'] not in sessions:
            sessions.append(row['session_id'])
        requests = trace(raw)
        methods = [r['method'] for r in requests if 'method' in r and r['method'] != '_orchestrator/background']
        state = board_store.get_task(task_id)
        messages = chat()['messages']
        detail = {'phase': name, 'seconds': round(time.monotonic()-started, 2), 'exit_code': code,
                  'status': state['status'], 'methods': methods, 'session_id': row['session_id'],
                  'model_ids': model_ids(raw),
                  'background_snapshots': sum(r.get('method') == '_orchestrator/background' for r in requests),
                  'stop_reason': row['stop_reason'], 'active_reset_rejected': observed.get('reset_status') == 409,
                  'history_preserved': messages[:len(before)] == before}
        phases.append(detail)
        emit({'agent': agent, 'event': 'phase_finished', **detail})
        assert observed.get('reset_status') == 409 and observed.get('hidden_while_running'), observed
        assert not observed.get('monitor_error') and not observed.get('timeout'), observed
        assert messages[:len(before)] == before, 'Existing message IDs/text/dates changed'
        if code != 0:
            # Return only known safe error classes, never arbitrary provider diagnostics.
            error = (row.get('error') or '')
            category = ('authentication_required' if any(word in error.lower() for word in ['auth', 'login', 'logged in', 'credential'])
                        else 'remote_execution_failed')
            detail['failure_category'] = category
            known = ['Authentication required', 'Not logged in', 'missing saved session id',
                     'missing session id', 'Invalid session id', 'Agent executor failed',
                     'Missing, invalid or unsafe sessionId',
                     'Remote agent timed out', 'Remote agent cancelled', 'Invalid remote agent configuration']
            detail['error_label'] = next((label for label in known if label.lower() in error.lower()), 'ACP request failed')
            detail['safe_error'] = env.safe_failure(error)
            return {'failed': True, 'category': category, 'uncertain': bool(row['agent_uncertain']), 'detail': detail}
        assert state['status'] == 'REVIEW' and row['stop_reason'] == 'end_turn', detail
        assert ('session/load' if expected_session else 'session/new') in methods, methods
        assert ('session/new' if expected_session else 'session/load') not in methods, methods
        if expected_session:
            assert row['session_id'] == expected_session
        # Independent process inspection follows the real worker's cleanup.
        remaining = env.processes(cwd)
        until = time.monotonic()+5
        while remaining and time.monotonic() < until:
            time.sleep(.2)
            remaining = env.processes(cwd)
        assert not remaining, 'Test-owned remote processes remain'
        detail['remaining_processes'] = len(remaining)
        return {'detail': detail, 'messages': messages, 'requests': requests}

    result = {'agent': agent, 'command': env.commands[agent], 'phases': phases, 'sessions': sessions}
    try:
        first = execute('initial', 'Remember this conversation-only token: '+nonce+'. '
                        'Do not write this token to any file. Write exactly '+artifact+' to result.txt, '
                        'read it back, then reply with the conversation-only token.')
        if first.get('failed'):
            if first['category'] == 'authentication_required':
                # Even without provider authentication, exercise the real reset
                # and prove that the next attempt creates a distinct ACP session.
                old_session = first['detail']['session_id']
                before = chat()
                assert before['can_clear_context'] is True
                cleared = client.post(base+'/clear-context')
                assert cleared.status_code == 200
                assert cleared.json()['messages'][:-1] == before['messages']
                assert cleared.json()['can_clear_context'] is False
                assert client.post(base+'/clear-context').json() == cleared.json()
                second = execute('new_after_auth_failure_clear', 'Reply AUTH_PROBE only; do not use tools.')
                assert second.get('failed'), 'Unauthenticated model attempt unexpectedly succeeded'
                assert 'session/new' in second['detail']['methods'] and 'session/load' not in second['detail']['methods']
                result.update(status='partial', reason='authentication_required',
                              new_session_requested=True,
                              new_session_created=bool(second['detail']['session_id'] and second['detail']['session_id'] != old_session),
                              reset_idempotent=True, reset_http_passed=True,
                              model_memory_check='not_run_without_existing_authentication')
                return result
            result.update(status='blocked' if first['category'] == 'authentication_required' else 'failed',
                          reason=first['category'], uncertain=first['uncertain'])
            return result
        old_session = first['detail']['session_id']
        assert nonce in '\n'.join(m['text'] for m in first['messages'] if m['role'] == 'agent'), 'Initial memory not acknowledged'
        assert env.read(cwd+'/result.txt') == artifact
        continued = execute('resume_before_clear', 'What was the conversation-only token I asked you to remember? '
                            'Reply with it exactly. Do not inspect files or external session stores.', old_session)
        assert not continued.get('failed'), continued
        assert nonce in '\n'.join(m['text'] for m in continued['messages'][len(first['messages']):] if m['role'] == 'agent')
        before = chat()
        assert before['can_clear_context'] is True
        response = client.post(base+'/clear-context')
        assert response.status_code == 200
        cleared = response.json()
        assert cleared['status'] == 'REVIEW' and cleared['can_clear_context'] is False
        assert cleared['messages'][:-1] == before['messages']
        assert cleared['messages'][-1]['role'] == 'system'
        assert client.post(base+'/clear-context').json() == cleared
        board_store.init_database()
        agent_store.init_database()
        assert chat() == cleared, 'Reset did not survive database initialization'
        fresh = execute('new_after_clear', 'If you know a conversation-only memory token from earlier messages, '
                        'reply with it; otherwise reply NO_PREVIOUS_CONTEXT. Do not guess it or inspect external history. '
                        'Read result.txt with a tool and include its contents in the answer. '
                        'Now remember the new conversation-only token '+new_nonce+' without saving it to files.')
        assert not fresh.get('failed'), fresh
        new_session = fresh['detail']['session_id']
        assert new_session != old_session, 'Reset reused the old remote session'
        fresh_text = '\n'.join(m['text'] for m in fresh['messages'][len(cleared['messages']):] if m['role'] == 'agent')
        assert 'NO_PREVIOUS_CONTEXT' in fresh_text and nonce not in fresh_text and artifact in fresh_text, 'New session did not demonstrate fresh memory and persistent file access'
        assert env.read(cwd+'/result.txt') == artifact
        resumed = execute('resume_after_clear', 'Reply with the new conversation-only token I asked you to remember. '
                          'Do not read files or external histories.', new_session)
        assert not resumed.get('failed'), resumed
        resumed_text = '\n'.join(m['text'] for m in resumed['messages'][len(fresh['messages']):] if m['role'] == 'agent')
        assert new_nonce in resumed_text and nonce not in resumed_text
        assert env.read(cwd+'/result.txt') == artifact
        result.update(status='passed', old_memory_resumed=True, new_session_created=True,
                      old_memory_absent=True, artifact_preserved=True, new_session_resumed=True,
                      reset_idempotent=True, reset_survives_startup=True)
        return result
    except Exception as error:
        result.update(status='failed', reason=type(error).__name__)
        # Assertion labels are deliberately free of model output and auth data.
        if isinstance(error, AssertionError) and error.args and isinstance(error.args[0], str):
            result['assertion'] = error.args[0][:300]
        return result
    finally:
        client.close()
        result['residual_processes'] = len(env.processes(cwd))
        if result['residual_processes']:
            env.processes(cwd, stop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=('claude', 'codex', 'qwen'), default=['claude', 'codex', 'qwen'])
    parser.add_argument('--phase-deadline', type=int, default=300)
    parser.add_argument('--claude-qwen-provider', action='store_true',
                        help='Temporarily connect Claude to the existing remote Qwen provider')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    original_db, original_files = board_store.DATABASE_PATH, board_store.FILES_DIR
    env = CurrentEnvironment()
    before_settings = env.settings_snapshot()
    env.cache_snapshot()
    report = {'date': time.strftime('%Y-%m-%d', time.gmtime()), 'remote_host': env.config['remote_server.host'],
              'settings_source': 'existing remote user settings and authentication; no provider/model overrides',
              'wait_background_tasks': env.config['agent.acp_wait_background_tasks'],
              'quiet_timeout': env.config['agent.acp_background_quiet_timeout'], 'results': []}
    emit({'event': 'started', 'remote_root': env.root})
    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    try:
        if args.claude_qwen_provider:
            report['claude_provider'] = env.configure_claude_qwen()
            report['settings_source'] = 'Claude temporarily uses existing remote Qwen API/model/credential; permanent settings unchanged'
            emit({'event': 'claude_provider', **report['claude_provider']})
        for agent in args.agents:
            with TemporaryDirectory(prefix='acp-context-live-') as temporary:
                storage = Path(temporary)
                board_store.DATABASE_PATH, board_store.FILES_DIR = storage/'test.db', storage/'files'
                board_store.init_database()
                agent_store.init_database()
                started = time.monotonic()
                result = run_agent(env, agent, storage, args.phase_deadline, emit)
                result['seconds'] = round(time.monotonic()-started, 2)
                report['results'].append(result)
                emit(result)
                save()
    finally:
        board_store.DATABASE_PATH, board_store.FILES_DIR = original_db, original_files
        report['cleanup'] = {}
        try:
            report['remote_settings_unchanged'] = env.settings_snapshot() == before_settings
            report['cleanup'].update(env.clean_caches([s for row in report['results'] for s in row['sessions']]))
            report['cleanup']['remaining_processes'] = len(env.processes())
        finally:
            env.cleanup()
            report['cleanup']['remote_root_removed'] = True
            save()
            # Dispose Paramiko file cycles before interpreter shutdown closes BytesIO.
            gc.collect()
    return int(any(row['status'] != 'passed' for row in report['results']))


if __name__ == '__main__':
    raise SystemExit(main())
