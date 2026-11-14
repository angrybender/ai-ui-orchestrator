"""Fork protocol exercised through real worker/ACP pipes and an isolated DB."""
import pytest

import agent_background_air
import agent_background_claude
import agent_cancel_fixture
import board_store
from test_agent_background import saved_run, wait_for
from test_agent_background_air import AGENT as AIR_AGENT

AGENT = AIR_AGENT.replace('threads = []', 'threads = []\ntasks = {}\nrevision = 0\nclose_attempts = 0')
AGENT = AGENT.replace("def update(kind, sid='local-session', **fields):", """def update(kind, sid='local-session', **fields):
    global revision
    revision += 1
    if kind == 'async_task_spawned': tasks[fields['asyncTaskId']] = 'running'
    if kind == 'async_task_state_update': tasks[fields['asyncTaskId']] = fields['state']""")
AGENT = AGENT.replace("if mode=='unsupported':", "result['_meta']['orchestratorBackgroundTasks']={'version':1}\n        if mode=='unsupported':")
AGENT = AGENT.replace("    elif m=='session/close':", """    elif m=='_orchestrator/background':
        pending = any(state in ('running', 'paused') for state in tasks.values())
        result = dict(version=1, sessionId='local-session', pending=pending, modelBusy=False,
                      token=str(revision), closed=False,
                      tasks=[dict(id=k,state=v,autonomous=False) for k,v in tasks.items()])
        Path('snapshot.ready').touch()
        if p.get('close'):
            close_attempts += 1
            if mode=='stale-close' and close_attempts==1:
                revision += 1
                Path('stale.refused').touch()
            elif not pending and p.get('token')==str(revision):
                Path('close.allowed').touch()
                if mode=='close-race':background('race','release.race')
                result['closed']=True
        if mode=='bad-snapshot':result['pending']='false'
    elif m=='session/close':""")
AGENT = AGENT.replace("            if mode=='second' and count==2:background('second','release.second')", """            if mode=='many' and count<=6:
                Path('release.next').touch()
                background('generation-'+str(count),'release.next')
            if mode=='second' and count==2:background('second','release.second')""")


@pytest.fixture
def fork_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cancel_fixture, 'AGENT', AGENT)
    monkeypatch.setattr(agent_background_claude, 'POLL_SECONDS', .025)
    harnesses = []
    def create(mode='normal'):
        h = agent_cancel_fixture.Harness(tmp_path, monkeypatch, mode)
        h.config['agent.acp_wait_background_tasks'] = True
        h.config['agent.acp_background_quiet_timeout'] = 1
        harnesses.append(h)
        return h
    yield create
    for h in harnesses:
        h.cleanup()


@pytest.mark.parametrize('mode', ['normal', 'failed', 'stale-close', 'child', 'no-jobs'])
def test_fork_drains_and_closes_without_signals(fork_harness, mode):
    h = fork_harness(mode)
    h.start()
    wait_for(h, 'snapshot.ready')
    assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
    assert not (h.cwd/'close.allowed').exists()
    (h.cwd/'release.first').touch()
    h.worker.join(8)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']
    assert saved_run()['agent_stop_confirmed'] == 1
    assert saved_run()['agent_uncertain'] == 0
    messages = board_store.get_chat('CANCEL-1')['messages']
    assert [m['text'] for m in messages] == (['started'] if mode == 'no-jobs' else
                                           ['started', 'verified final answer'])
    assert len({m['run_id'] for m in messages}) == 1
    if mode == 'stale-close':
        assert (h.cwd/'stale.refused').exists()
    assert all('kill -TERM' not in c.process.args for c in h.ssh.channels[1:])


def test_fork_allows_more_than_three_result_generations(fork_harness, monkeypatch):
    monkeypatch.setattr(agent_background_air, 'MAX_FINALIZATION_TURNS', 0)
    monkeypatch.setattr(agent_background_air, 'MAX_RECONCILE_SECONDS', .01)
    h = fork_harness('many')
    h.start()
    (h.cwd/'release.first').touch()
    h.worker.join(10)
    assert not h.worker.is_alive()
    assert (h.cwd/'verify.7').exists()
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']

    messages = board_store.get_chat('CANCEL-1')['messages']
    assert [m['text'] for m in messages] == ['started'] + ['verified final answer'] * 6


def test_fork_does_not_apply_the_legacy_drain_deadline(fork_harness, monkeypatch):
    monkeypatch.setattr(agent_background_air, 'MAX_RECONCILE_SECONDS', .01)
    h = fork_harness()
    h.start()
    wait_for(h, 'snapshot.ready')
    h.worker.join(1.3)
    assert h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'IN PROGRESS'
    (h.cwd/'release.first').touch()
    h.worker.join(8)
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']


def test_fork_rejects_malformed_snapshot(fork_harness):
    h = fork_harness('bad-snapshot')
    h.start()
    (h.cwd/'release.first').touch()
    h.worker.join(8)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'WAIT'
    assert 'snapshot' in saved_run()['error']


def test_fork_reconciliation_does_not_retry_unchanged_work(fork_harness, monkeypatch):
    monkeypatch.setattr(agent_background_claude, 'RECONCILE_SECONDS', .05)
    h = fork_harness('stubborn')
    h.start()
    wait_for(h, 'verify.2')
    h.worker.join(.4)
    assert h.worker.is_alive()
    assert not (h.cwd/'verify.3').exists()
    (h.cwd/'release.first').touch()
    h.worker.join(8)
    assert board_store.get_task('CANCEL-1')['status'] == 'REVIEW', saved_run()['error']


@pytest.mark.parametrize('cancel', [True, False])
def test_fork_preserves_cancellation_and_overall_deadline(fork_harness, cancel):
    h = fork_harness()
    if not cancel:
        h.config['agent.agent_timeout'] = 2
    h.start()
    wait_for(h, 'snapshot.ready')
    if cancel:
        board_store.move_task('CANCEL-1', 'BACKLOG', 0)
    h.worker.join(8)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == ('BACKLOG' if cancel else 'WAIT')
    assert saved_run()['state'] == 'FAILED'
    if cancel:
        assert saved_run()['stop_reason'] == 'cancelled'
        assert saved_run()['agent_uncertain'] == 0
    else:
        assert 'timed out' in saved_run()['error']


def test_fork_does_not_accept_new_work_during_confirmed_close(fork_harness):
    h = fork_harness('close-race')
    h.start()
    (h.cwd/'release.first').touch()
    h.worker.join(8)
    assert not h.worker.is_alive()
    assert board_store.get_task('CANCEL-1')['status'] == 'WAIT'
    assert 'work appeared' in saved_run()['error']


@pytest.mark.parametrize('recovers', [True, False])
def test_fork_rechecks_transient_process_verification_without_signals(fork_harness, monkeypatch, recovers):
    import agent_control
    original = agent_control.stop_group
    attempts = []
    def verify(transport, pid, *, terminate=True, **options):
        if terminate:
            return original(transport, pid, terminate=True, **options)
        attempts.append(pid)
        if len(attempts) == 1 or not recovers:
            return False
        return original(transport, pid, terminate=False)
    monkeypatch.setattr(agent_control, 'stop_group', verify)
    h = fork_harness('no-jobs')
    h.start()
    h.worker.join(8)
    assert not h.worker.is_alive()
    assert len(attempts) == 2
    assert board_store.get_task('CANCEL-1')['status'] == ('REVIEW' if recovers else 'WAIT')
    assert saved_run()['agent_uncertain'] == (0 if recovers else 1)
    assert all('kill -TERM' not in c.process.args for c in h.ssh.channels[1:])
