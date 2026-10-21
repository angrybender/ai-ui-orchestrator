"""Opt-in ACP draining through real local processes and isolated task storage."""
import threading
import time
from contextlib import closing

import pytest

import agent_background
import agent_cancel_fixture
import agent_store
import board_store
from agent_cancel_fixture import Harness, alive


AGENT = r'''
import json, os, subprocess, sys, threading, time
from pathlib import Path
mode = sys.argv[1]
lock = threading.Lock()
child = None
settled = False
def emit(message):
    with lock:
        print(json.dumps(dict(jsonrpc='2.0', **message)), flush=True)
def update(text, source=None):
    value = {'sessionUpdate':'agent_message_chunk', 'content':{'type':'text','text':text}}
    if source: value['_meta'] = {'source':source}
    emit({'method':'session/update','params':{'sessionId':'local-session','update':value}})
def complete():
    global settled, child
    child.wait()
    if mode == 'second-job':
        child = launch('release.second', 'second.done')
        Path('second.ready').touch()
        child.wait()
    update('Background task completed.', 'background_notification')
    Path('notification.ready').touch()
    while not Path('release.summary').exists(): time.sleep(.01)
    update('verified final ', 'background_notification_response')
    update('answer', 'background_notification_response')
    emit({'method':'_qwencode/end_turn','params':{'sessionId':'local-session',
          'reason':'error' if mode == 'automatic-error' else 'end_turn',
          'source':'background_notification'}})
    settled = True
def launch(gate, output):
    code = ('import time; from pathlib import Path; '
            f'gate=Path({gate!r});\nwhile not gate.exists(): time.sleep(.01)\n'
            f'Path({output!r}).write_text("complete"); '
            f'raise SystemExit({7 if mode == "failed" else 0})')
    result = subprocess.Popen([sys.executable, '-c', code])
    Path('child.pid').write_text(str(result.pid))
    return result
try:
    for line in sys.stdin:
        r=json.loads(line); method=r['method']; params=r.get('params',{})
        if method=='initialize':
            result={'protocolVersion':1}
            if mode!='unsupported': result['_meta']=params.get('_meta',{})
        elif method=='session/load':
            update('old replay must stay hidden')
            result={}
        elif method=='session/new': result={'sessionId':'local-session'}
        elif method=='session/prompt':
            if mode!='no-jobs':
                child=launch('release.child', 'child.done')
                threading.Thread(target=complete,daemon=True).start()
            else: settled=True
            update('launched')
            Path('prompt.ready').touch()
            result={'stopReason':'end_turn'}
        elif method=='qwen/status/session/tasks':
            Path('status.polled').touch()
            if mode=='disconnect': sys.exit(0)
            task=[] if child is None else [{'kind':'shell','id':'bg-1',
                   'status':'running' if child.poll() is None else ('failed' if child.returncode else 'completed'),
                   **({'exitCode':child.returncode} if child.poll() is not None else {})}]
            result={'v':1,'sessionId':'local-session','tasks':task if mode!='bad-snapshot' else 'invalid'}
        elif method=='qwen/control/session/close':
            if not params.get('onlyIfUnheld'):
                Path('force.closed').touch()
                if child and child.poll() is None: child.terminate(); child.wait()
                result={'sessionId':'local-session','closed':True}
            elif not settled:
                Path('close.refused').touch()
                result={'sessionId':'local-session','closed':False,
                        'holds':[{'category':'shell','id':'background-shells'}]}
            elif mode=='close-error' and not Path('close.error').exists():
                Path('close.error').touch()
                emit({'id':r['id'],'error':{'code':-32603,'message':'Session drain timed out'}})
                continue
            else:
                Path('close.allowed').touch()
                result={'sessionId':'local-session','closed':True}
        elif method=='session/cancel':
            Path('cancel.received').touch()
            continue
        else: raise RuntimeError(method)
        emit({'id':r['id'],'result':result})
finally:
    if child and child.poll() is None: child.terminate(); child.wait()
'''


@pytest.fixture
def harness_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cancel_fixture, 'AGENT', AGENT)
    monkeypatch.setattr(agent_background, 'POLL_SECONDS', 0.03)
    harnesses = []

    def create(mode='normal', enabled=True):
        harness = Harness(tmp_path, monkeypatch, mode)
        if enabled is not None:
            harness.config['agent.acp_wait_background_tasks'] = enabled
        harnesses.append(harness)
        return harness
    yield create
    for harness in harnesses:
        harness.cleanup()


def wait_for(harness, name):
    deadline = time.monotonic() + 5
    while not (harness.cwd / name).exists():
        assert time.monotonic() < deadline, name
        assert harness.worker.is_alive(), name
        time.sleep(0.01)


def saved_run():
    with closing(board_store.connect()) as db:
        return dict(db.execute('SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 1').fetchone())


@pytest.mark.parametrize('mode', ['normal', 'second-job', 'close-error'])
def test_waits_for_jobs_and_automatic_result_before_review(harness_factory, mode):
    h = harness_factory(mode)
    h.start()
    wait_for(h, 'status.polled')
    assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
    assert agent_store.claim(60) is None
    assert not (h.cwd / 'child.done').exists()
    # Save affects the next claim, not the already captured configuration.
    h.config['agent.acp_wait_background_tasks'] = False
    (h.cwd / 'release.child').touch()
    if mode == 'second-job':
        wait_for(h, 'second.ready')
        assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
        (h.cwd / 'release.second').touch()
    wait_for(h, 'notification.ready')
    wait_for(h, 'close.refused')
    assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
    assert h.worker.is_alive()
    (h.cwd / 'release.summary').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert (h.cwd / 'close.allowed').exists()
    assert not (h.cwd / 'force.closed').exists()
    assert not alive(int((h.cwd / 'child.pid').read_text()))
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW'
    assert board_store.get_chat('CANCEL-1')['messages'][-1]['text'] == 'verified final answer'
    assert saved_run()['agent_stop_confirmed'] == 1
    assert saved_run()['agent_uncertain'] == 0
    assert all('kill -TERM' not in channel.process.args for channel in h.ssh.channels[1:])
    if mode == 'close-error':
        assert 'Session drain timed out' in saved_run()['log']


@pytest.mark.parametrize('enabled', [False, None])
def test_disabled_and_legacy_configuration_keep_immediate_completion(harness_factory, enabled):
    h = harness_factory(enabled=enabled)
    h.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW'
    assert not (h.cwd / 'status.polled').exists()
    assert not (h.cwd / 'close.allowed').exists()
    assert not (h.cwd / 'child.done').exists()
    assert not alive(int((h.cwd / 'child.pid').read_text()))


@pytest.mark.parametrize('mode', ['failed', 'automatic-error', 'bad-snapshot', 'disconnect'])
def test_background_failures_never_reach_review(harness_factory, mode):
    h = harness_factory(mode)
    h.start()
    (h.cwd / 'release.child').touch()
    (h.cwd / 'release.summary').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    task = board_store.get_task('CANCEL-1')
    assert task['status'] == 'WAIT' and task['is_error']
    assert saved_run()['state'] == 'FAILED'
    assert not (h.cwd / 'close.allowed').exists()


def test_unsupported_agent_fails_before_prompt(harness_factory):
    h = harness_factory('unsupported')
    h.worker.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert not (h.cwd / 'prompt.ready').exists()
    assert board_store.get_task('CANCEL-1')['status'] == 'WAIT'
    assert 'requires Qwen active-work support' in saved_run()['error']


def test_no_background_work_completes_without_delay(harness_factory):
    h = harness_factory('no-jobs')
    h.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert (h.cwd / 'close.allowed').exists()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW'


def test_enabled_resume_suppresses_replayed_history(harness_factory):
    import agent_worker
    h = harness_factory('no-jobs')
    h.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    previous = board_store.get_chat('CANCEL-1')['messages'][:]
    board_store.add_user_comment('CANCEL-1', 'Continue this session')
    h.ssh = agent_cancel_fixture.LocalSSH()
    h.worker = threading.Thread(target=agent_worker.execute)
    h.worker.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    chat = board_store.get_chat('CANCEL-1')
    assert chat['status'] == 'REVIEW', saved_run()['error']
    assert chat['messages'][:len(previous)] == previous
    assert all('old replay' not in message['text'] for message in chat['messages'])


def test_timeout_keeps_original_deadline(harness_factory):
    h = harness_factory()
    h.config['agent.agent_timeout'] = 0.4
    started = time.monotonic()
    h.start()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert 0.4 <= time.monotonic() - started < 4
    assert board_store.get_task('CANCEL-1')['status'] == 'WAIT'
    assert 'timed out' in saved_run()['error']
    assert not alive(int((h.cwd / 'child.pid').read_text()))


def test_cancel_during_background_wait_closes_registry_and_preserves_backlog(harness_factory):
    h = harness_factory()
    h.start()
    wait_for(h, 'status.polled')
    board_store.move_task('CANCEL-1', 'BACKLOG', 0)
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'BACKLOG'
    assert saved_run()['stop_reason'] == 'cancelled'
    assert (h.cwd / 'force.closed').exists()
    assert saved_run()['agent_uncertain'] == 0


def test_checkbox_defaults_and_roundtrip(tmp_path):
    from settings import settings as store
    from settings.config import Config
    section = next(s for s in store.load_sections() if s['key'] == 'agent')
    assert Config.get('agent.acp_wait_background_tasks') is False
    values = {'shell': 'qwen --acp', 'agent_timeout': 60, 'prompt': 'Complete work'}
    for enabled in (True, False):
        normalized = store.validate_values(section, {**values, 'acp_wait_background_tasks': enabled})
        store.save_values('agent', normalized)
        assert Config.get('agent.acp_wait_background_tasks') is enabled


@pytest.mark.parametrize('task', [
    {}, {'status': 'completed'},
    {'id': 'x', 'kind': 'unknown', 'status': 'completed'},
    {'id': 'x', 'kind': 'shell', 'status': 'completed'},
    {'id': 'x', 'kind': 'shell', 'status': 'completed', 'exitCode': True},
    {'id': 'x', 'kind': 'shell', 'status': 'completed', 'exitCode': 7},
])
def test_incomplete_or_failed_task_cannot_authorize_close(task):
    # No I/O is required: the untrusted snapshot must be rejected before close.
    background = object.__new__(agent_background.BackgroundACP)
    background.cancelled = threading.Event()
    background.requested = threading.Event()
    background.failure = None
    background.session_id = 's'
    calls = []
    def request(method, params):
        calls.append(method)
        return {'v': 1, 'sessionId': 's', 'tasks': [task]}
    with pytest.raises(ValueError, match='ACP background'):
        background.wait(request, RuntimeError, lambda _: None)
    assert calls == ['qwen/status/session/tasks']
