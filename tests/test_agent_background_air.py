"""AIR lifecycle regressions through the actual worker, ACP pipes and task DB."""
import threading
import time

import pytest

import agent_background_air
import agent_cancel_fixture
import agent_store
import board_store
from test_agent_background import saved_run, wait_for


AGENT = r'''
import json, sys, threading, time
from pathlib import Path
mode = sys.argv[1]
lock = threading.Lock()
count = 0
threads = []
def emit(value):
    with lock: print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)
def update(kind, sid='local-session', **fields):
    emit({'method':'session/update','params':{'sessionId':sid,'update':dict(sessionUpdate=kind,**fields)}})
def text(value): update('agent_message_chunk',content={'type':'text','text':value})
def background(task, gate, child=False):
    sid = 'child' if child else 'local-session'
    if child: update('subagent_spawned',subagentSessionId=sid,name='child',task='work',capabilities={})
    update('async_task_spawned',sid=sid,asyncTaskId=task,taskType='shell',name='job',canStop=True,showInTranscript=False)
    # Completed Bash launches must not release the async task.
    update('tool_call_update',sid=sid,toolCallId=task,status='completed')
    def finish():
        while not Path(gate).exists() and not Path('force.closed').exists():
            if mode=='chatter':update('async_task_progress',sid=sid,asyncTaskId=task,description='still running')
            time.sleep(.01)
        if Path('force.closed').exists() or Path(task+'.stopped').exists(): return
        Path(task+'.done').touch()
        if mode in ('stop-correction','stopped'):
            update('async_task_state_update',sid=sid,asyncTaskId=task,state='stopped')
            time.sleep(.1)
        if mode!='stopped':
            update('async_task_state_update',sid=sid,asyncTaskId=task,state='failed' if mode.startswith('failed') else 'completed')
        if mode=='failed-correction':
            update('async_task_state_update',sid=sid,asyncTaskId=task,state='completed')
        if child:update('subagent_state_update',subagentSessionId=sid,state='completed')
        if mode=='late':
            time.sleep(.2);text('late automatic summary');Path('late.ready').touch()
        if mode=='late-task' and task=='first':
            time.sleep(.25);background('second','release.second');Path('late.task').touch()
    t=threading.Thread(target=finish,daemon=True);threads.append(t);t.start()
try:
 for line in sys.stdin:
    r=json.loads(line);m=r['method'];p=r.get('params',{})
    with open('requests.jsonl','a') as f:f.write(json.dumps(r)+'\n')
    if m=='initialize':
        result={'protocolVersion':1,'agentCapabilities':{'sessionCapabilities':{'close':{}}},
                '_meta':{'jetbrains':{'air':{'version':1,'capabilities':['asyncTasks','nativeSubagentSessions','sessionFailure']}}}}
        if mode=='unsupported':result['agentCapabilities']={}
    elif m=='session/new': result={'sessionId':'local-session'}
    elif m=='session/load':
        update('subagent_spawned',subagentSessionId='historical-child',name='old',task='old',capabilities={})
        update('subagent_state_update',subagentSessionId='historical-child',state='failed')
        background('old','release.old')
        update('async_task_state_update',asyncTaskId='old',state='failed')
        text('old replay text')
        result={}
    elif m=='session/prompt':
        count+=1
        if count==1:
            Path('prompt.ready').touch()
            if mode=='failed-before-end':
                Path('release.first').touch()
            if mode not in ('no-jobs','silent','provider-error'):
                background('first','release.first',mode=='child')
            if mode=='failed-server':background('server','release.server')
            if mode=='failed-before-end':
                for t in threads:t.join()
            text('started')
        else:
            Path('verify.'+str(count)).touch()
            if mode=='silent-final':continue
            if count==2 and mode in ('server-stop-before','server-stop-after','server-stop-failed','failed-server'):
                target='server' if mode=='failed-server' else 'first'
                update('tool_call',toolCallId='stop',name='TaskStop',status='pending')
                update('tool_call_update',toolCallId='stop',rawInput={'task_id':target})
                Path(target+'.stopped').touch();Path('release.'+target).touch()
                if mode!='server-stop-after':update('async_task_state_update',asyncTaskId=target,state='stopped')
                update('tool_call_update',toolCallId='stop',status='failed' if mode=='server-stop-failed' else 'completed')
                if mode=='server-stop-after':update('async_task_state_update',asyncTaskId=target,state='stopped')
            if mode=='second' and count==2:background('second','release.second')
            text('verified final answer')
        if mode=='silent':update('usage_update',used=1,size=100)
        if mode=='disconnect':sys.exit(0)
        result={'stopReason':'refusal' if mode in ('refusal','failed-refusal') and count>1 else 'end_turn'}
        if mode=='provider-error':result['_meta']={'jetbrains':{'air':{'sessionFailure':{'severity':'error'}}}}
    elif m=='session/close':
        if mode=='silent-close':continue
        if mode=='close-race':background('race','release.race')
        Path('force.closed' if any(t.is_alive() for t in threads) and not Path('first.done').exists() else 'close.allowed').touch()
        result={}
    elif m=='session/cancel':Path('cancel.received').touch();continue
    else:raise RuntimeError('unexpected method '+m)
    emit({'id':r['id'],'result':result})
    if m=='session/close' and mode=='hung-shutdown':time.sleep(60)
    if m=='session/close' and mode=='postclose-race':background('late-race','release.late-race')
finally:
 Path('peer.exited').touch()
'''


@pytest.fixture
def air_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cancel_fixture, 'AGENT', AGENT)
    # Keep real monotonic timing and integer seconds; these are short real waits.
    harnesses = []
    def create(mode='normal', enabled=True, timeout=1):
        h = agent_cancel_fixture.Harness(tmp_path, monkeypatch, mode)
        h.config['agent.acp_wait_background_tasks'] = enabled
        h.config['agent.acp_background_quiet_timeout'] = timeout
        harnesses.append(h)
        return h
    yield create
    for h in harnesses: h.cleanup()


@pytest.mark.parametrize('mode', ['normal', 'child', 'second', 'late', 'late-task', 'stop-correction'])
def test_waits_for_results_rechecks_new_work_and_quiet_period(air_harness, mode):
    h = air_harness(mode)
    h.start()
    assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
    assert not (h.cwd/'verify.2').exists()
    (h.cwd/'release.first').touch()
    wait_for(h,'verify.2')
    assert agent_store.claim(30) is None
    if mode=='late-task':wait_for(h,'late.task')
    if mode in ('second','late-task'):
        assert not (h.cwd/'second.done').exists()
        assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
        (h.cwd/'release.second').touch()
        wait_for(h,'verify.3')
    if mode=='late':wait_for(h,'late.ready')
    start=time.monotonic()
    assert not (h.cwd/'close.allowed').exists()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert time.monotonic()-start >= .7
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']
    assert (h.cwd/'close.allowed').exists() and (h.cwd/'peer.exited').exists()
    assert saved_run()['agent_uncertain'] == 0
    assert saved_run()['agent_stop_confirmed'] == 1
    assert all('kill -TERM' not in c.process.args for c in h.ssh.channels[1:])
    requests=(h.cwd/'requests.jsonl').read_text()
    assert 'qwen/status' not in requests and 'qwen/control' not in requests


@pytest.mark.parametrize('mode', ['failed-refusal','provider-error','refusal','disconnect','stopped','close-race','postclose-race'])
def test_failure_cannot_reach_review(air_harness, mode):
    h=air_harness(mode);h.start();(h.cwd/'release.first').touch();h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert saved_run()['state']=='FAILED'


@pytest.mark.parametrize('mode', ['failed', 'failed-before-end', 'failed-correction'])
def test_failed_command_is_processed_by_model(air_harness, mode):
    h = air_harness(mode)
    h.start()
    (h.cwd/'release.first').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']
    assert saved_run()['state'] == 'SUCCEEDED'
    assert (h.cwd/'verify.2').exists()
    assert saved_run()['agent_uncertain'] == 0


@pytest.mark.parametrize('mode', ['server-stop-before', 'server-stop-after', 'failed-server'])
def test_pending_server_can_be_stopped_during_bounded_finalization(air_harness, monkeypatch, mode):
    monkeypatch.setattr(agent_background_air, 'IDLE_RECONCILE_SECONDS', .15)
    h = air_harness(mode)
    h.start()
    if mode=='failed-server':(h.cwd/'release.first').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert (h.cwd/'verify.2').exists()
    assert (h.cwd/('server.stopped' if mode=='failed-server' else 'first.stopped')).exists()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']
    assert saved_run()['agent_stop_confirmed'] == 1
    assert saved_run()['agent_uncertain'] == 0


def test_unconfirmed_taskstop_cannot_authorize_review(air_harness, monkeypatch):
    monkeypatch.setattr(agent_background_air, 'IDLE_RECONCILE_SECONDS', .15)
    h=air_harness('server-stop-failed');h.start();h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'stop was not resolved' in saved_run()['error']


def test_stubborn_server_stops_after_three_finalization_turns(air_harness, monkeypatch):
    monkeypatch.setattr(agent_background_air, 'IDLE_RECONCILE_SECONDS', .15)
    h=air_harness('stubborn');h.start();h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'finalization limit reached' in saved_run()['error']
    assert (h.cwd/'verify.4').exists()
    assert not (h.cwd/'verify.5').exists()
    assert saved_run()['agent_uncertain']==0


@pytest.mark.parametrize('mode', ['chatter','silent-final'])
def test_reconciliation_deadline_bounds_progress_and_silent_rpc(air_harness, monkeypatch, mode):
    monkeypatch.setattr(agent_background_air, 'IDLE_RECONCILE_SECONDS', .15)
    monkeypatch.setattr(agent_background_air, 'MAX_RECONCILE_SECONDS', .2)
    h=air_harness(mode);h.start()
    if mode=='silent-final':(h.cwd/'release.first').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'background reconciliation timed out' in saved_run()['error']
    assert saved_run()['agent_uncertain']==0


@pytest.mark.parametrize('enabled', [False, None])
def test_disabled_does_not_negotiate_air_or_wait(air_harness, enabled):
    import json
    h=air_harness(enabled=enabled);h.start();h.worker.join(5)
    assert board_store.get_task('CANCEL-1')['status']=='REVIEW'
    requests=[json.loads(l) for l in (h.cwd/'requests.jsonl').read_text().splitlines()]
    assert requests[0]['params']['clientCapabilities']=={}
    assert all(r['method']!='session/close' for r in requests)
    assert not (h.cwd/'verify.2').exists()


def test_cancel_during_quiet_does_not_wait_full_interval(air_harness):
    h=air_harness('no-jobs',timeout=60);h.start()
    board_store.move_task('CANCEL-1','BACKLOG',0)
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='BACKLOG'
    assert saved_run()['stop_reason']=='cancelled'
    assert saved_run()['agent_uncertain']==0


def test_total_deadline_includes_quiet_wait(air_harness):
    h=air_harness('no-jobs',timeout=60);h.config['agent.agent_timeout']=.4
    h.start();h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'timed out' in saved_run()['error']


def test_bad_timeout_rejected_before_prompt(air_harness):
    h=air_harness(timeout=True);h.worker.start();h.worker.join(5)
    assert not (h.cwd/'prompt.ready').exists()
    assert 'positive integer' in saved_run()['error']


def test_no_jobs_still_waits_configured_quiet_period(air_harness):
    h=air_harness('no-jobs');h.start();start=time.monotonic();h.worker.join(5)
    assert time.monotonic()-start >= .8
    assert board_store.get_task('CANCEL-1')['status']=='REVIEW'
    assert not (h.cwd/'verify.2').exists()


def test_resume_rebuilds_registry_without_failing_on_historical_errors(air_harness):
    import agent_worker
    h=air_harness('no-jobs');h.start();h.worker.join(5)
    assert board_store.get_task('CANCEL-1')['status']=='REVIEW'
    board_store.add_user_comment('CANCEL-1','Continue the task')
    h.ssh=agent_cancel_fixture.LocalSSH()
    h.worker=threading.Thread(target=agent_worker.execute)
    h.worker.start();h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='REVIEW',saved_run()['error']
    assert all('old replay' not in m['text'] for m in board_store.get_chat('CANCEL-1')['messages'])


def test_air_requires_close_support_before_prompt(air_harness):
    h=air_harness('unsupported');h.worker.start();h.worker.join(5)
    assert not (h.cwd/'prompt.ready').exists()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'ACP AIR async-task support' in saved_run()['error']


def test_silent_close_and_cleanup_are_bounded(air_harness, monkeypatch):
    import agent_background
    monkeypatch.setattr(agent_background_air, 'MAX_RECONCILE_SECONDS', .2)
    monkeypatch.setattr(agent_background, 'CLEANUP_TIMEOUT_SECONDS', .2)
    h=air_harness('silent-close');h.start();(h.cwd/'release.first').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert saved_run()['agent_uncertain']==1
    assert 'timed out' in saved_run()['error']


def test_acknowledged_close_cannot_hang_acp_exit(air_harness, monkeypatch):
    monkeypatch.setattr(agent_background_air, 'SHUTDOWN_TIMEOUT_SECONDS', .2)
    h=air_harness('hung-shutdown');h.start();(h.cwd/'release.first').touch()
    h.worker.join(5)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status']=='WAIT'
    assert 'ACP AIR shutdown timed out.' in saved_run()['error']
    assert saved_run()['agent_uncertain']==0
