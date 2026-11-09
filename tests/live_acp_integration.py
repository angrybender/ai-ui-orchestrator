"""Real run_remote checks, with fresh local storage and remote test directories."""
from contextlib import closing
import json
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import threading
import time

import agent_remote
import agent_store
import board_store
from task_cycle import TaskCycle


def run_one(env, agent, case, *, waiting=True, invalid=False):
    base = env.root+'/integration-'+agent
    try:
        env.sftp.mkdir(base, 0o700)
    except OSError:
        pass
    task_id = 'LIVE-'+agent.upper()+'-'+case+('-OFF' if not waiting else '')
    cwd = base+'/'+task_id
    prompts = {
        'INT-01': 'Launch one necessary native background shell task: `sleep 18; printf INTEGRATION_OK > result.txt`. Return an early initial response, then when asked to finish read result.txt and verify INTEGRATION_OK. Do not stop or duplicate this finite work.',
        'INT-02': ('Create server.py with this Python code: import http.server,pathlib,os; s=http.server.HTTPServer(("127.0.0.1",0),http.server.SimpleHTTPRequestHandler); pathlib.Path("server.pid").write_text(str(os.getpid())); pathlib.Path("server.port").write_text(str(s.server_port)); s.serve_forever(). '
                   'Launch TWO separate native background tasks: `python3 -u server.py` and `sleep 6; printf EXPECTED_ERROR >&2; exit 7`. '
                   'Return an early initial response. On finalization, inspect the intentional exit 7 without retrying it, read server.port and verify HTTP 200, '
                   'stop only this disposable server using native task stop when available or its recorded server.pid, verify its exit and write RECONCILED_OK to result.txt.'),
        'INT-03': ('Launch native background command `sleep 10; printf FIRST_OK > first.txt`, then return an early initial response. '
                   'When asked to finalize, read first.txt, then launch one more native background command `sleep 10; printf SECOND_OK > second.txt`. '
                   'Do not finish the original task until you have read both files and written REPEAT_OK to result.txt.'),
        'INT-04': 'Launch the native shell command `printf STARTED > started.txt; sleep 120; printf LATE_RESULT > late.txt`. Do not stop or detach it. This test intentionally times out before it completes.',
        'INT-05': 'Launch native background shell command `printf STARTED > started.txt; sleep 120; printf LATE_RESULT > late.txt`, then return an early response. Never stop or detach it yourself.',
        'INT-06': 'Use a foreground native shell command to write exactly DISABLED_OK to result.txt. Read and verify the file before finishing. Do not start background tasks.',
        'INT-07': 'Reply AUTH_TEST without using tools.',
    }
    config = {**env.config, 'tasks.base_dir': base,
              'tasks.init_script_path': '', 'tasks.init_script_text': '', 'tasks.init_script_timeout': 300,
              'agent.agent_timeout': 25 if case in ('INT-04', 'INT-07') else 240,
              'agent.prompt': '', 'agent.continue_session_arg': '',
              'agent.acp_wait_background_tasks': waiting, 'agent.acp_background_quiet_timeout': 60}
    launch = ['python3', env.root+'/launch.py', env.root, agent]
    config['agent.shell'] = shlex.join(launch)+' "$PWD" '+('invalid' if invalid else 'valid')
    if agent == 'claude':
        instruction = 'Use native Bash with run_in_background=true for requested background work. '
    else:
        instruction = 'Use native exec_command with yield_time_ms=1000 for requested background work. '
    description = instruction+prompts[case]+' Work only in the current task directory; never use &, nohup, setsid, or detached subprocesses.'
    for filename in ('result.txt', 'server.py', 'server.pid', 'server.port', 'first.txt', 'second.txt', 'started.txt', 'late.txt'):
        description = description.replace(filename, cwd+'/'+filename)
    description += ' Use these absolute artifact paths and shell workdir '+cwd+'. Output files belong in the task directory, not its requirements directory.'
    saved_database, saved_files = board_store.DATABASE_PATH, board_store.FILES_DIR
    open_log = agent_remote._open_raw_log
    ssh_constructor = agent_remote.paramiko.SSHClient
    original_request = agent_remote._request
    stop_monitor = threading.Event()
    monitor = None
    captured = []
    started = time.monotonic()
    try:
        with TemporaryDirectory(prefix='acp-live-integration-') as temporary:
            storage = Path(temporary)
            board_store.DATABASE_PATH = storage/'test.db'
            board_store.FILES_DIR = storage/'files'
            board_store.init_database()
            agent_store.init_database()
            board_store.create_task(task_id, 'Live '+case, description, [])
            board_store.move_task(task_id, 'OPEN', 0)
            run = agent_store.claim(config['agent.agent_timeout'])
            task = board_store.get_task(task_id)
            task['_cycle'] = TaskCycle(run, config)
            task['session_id'] = None
            agent_remote._open_raw_log = lambda _: (storage/'raw.log').open('ab')
            if agent == 'codex' and case == 'INT-02':
                # ACP mode is explicit test setup, as in ACP-20. Setting only
                # sandbox_mode in Codex config does not select the ACP mode.
                def full_access(*args, **kwargs):
                    result = original_request(*args, **kwargs)
                    if args[3] == 'session/new':
                        changed = list(args)
                        changed[2] += 900000
                        changed[3] = 'session/set_mode'
                        changed[4] = {'sessionId': result['sessionId'], 'modeId': 'agent-full-access'}
                        original_request(*changed, **kwargs)
                    return result
                agent_remote._request = full_access
            if case == 'INT-05':
                def capture_ssh():
                    client = ssh_constructor()
                    captured.append(client)
                    return client

                agent_remote.paramiko.SSHClient = capture_ssh

                def disconnect_after_start():
                    while not stop_monitor.wait(.15):
                        if env.read(cwd+'/started.txt') == 'STARTED' and captured:
                            captured[0].close()
                            return
                monitor = threading.Thread(target=disconnect_after_start, daemon=True)
                monitor.start()
            error = None
            response = None
            try:
                response = agent_remote.run_remote(task, [], config,
                    on_session=lambda value: agent_store.session(run['id'], value),
                    on_log=lambda value: agent_store.append_log(run['id'], value),
                    on_started=lambda: agent_store.started(run['id']),
                    on_message=lambda value: agent_store.message(run['id'], value))
            except agent_remote.RemoteAgentError as failure:
                error = failure
            finally:
                stop_monitor.set()
                if monitor:
                    monitor.join(3)
            agent_store.finish(run['id'], str(error) if error else None, agent_uncertain=bool(error and error.uncertain))
            elapsed = round(time.monotonic()-started, 2)
            task_state = board_store.get_task(task_id)
            remaining = env.processes(cwd)
            if case in ('INT-04', 'INT-05', 'INT-07'):
                assert error is not None, {'false_success': response, 'task_status': task_state['status'], 'waiting': waiting}
                assert task_state['status'] == 'WAIT', task_state['status']
                if case == 'INT-04':
                    assert 'timed out' in str(error).lower(), str(error)
                    assert elapsed < 55, elapsed
                if case == 'INT-05':
                    assert env.read(cwd+'/started.txt') == 'STARTED', 'SSH disruption did not observe actual command startup'
                if error.uncertain:
                    board_store.create_task(task_id+'-NEXT', 'Blocked next task', 'Not run', [])
                    board_store.move_task(task_id+'-NEXT', 'OPEN', 0)
                    assert agent_store.claim(30) is None, 'Unconfirmed remote stop did not block retries'
                if remaining:
                    env.processes(cwd, stop=True)
                return {'error': env.safe(error), 'uncertain': error.uncertain, 'residual_processes': len(remaining), 'waiting': waiting, 'seconds': elapsed}
            assert error is None, env.safe(error)
            assert response['stopReason'] == 'end_turn', response
            expected = {'INT-01': 'INTEGRATION_OK', 'INT-02': 'RECONCILED_OK', 'INT-03': 'REPEAT_OK', 'INT-06': 'DISABLED_OK'}[case]
            actual = env.read(cwd+'/result.txt')
            assert actual is not None and actual.strip() == expected, {'expected': expected, 'actual': actual,
                'chat': [item['text'] for item in board_store.get_chat(task_id)['messages']][-1:]}
            if case == 'INT-03':
                assert env.read(cwd+'/first.txt') == 'FIRST_OK'
                assert env.read(cwd+'/second.txt') == 'SECOND_OK'
            assert task_state['status'] == 'REVIEW', task_state['status']
            assert not remaining, {'residual_processes': remaining}
            return {'result': expected, 'waiting': waiting, 'seconds': elapsed}
    finally:
        stop_monitor.set()
        agent_remote.paramiko.SSHClient = ssh_constructor
        agent_remote._request = original_request
        agent_remote._open_raw_log = open_log
        board_store.DATABASE_PATH, board_store.FILES_DIR = saved_database, saved_files


def integration(env, agent, case):
    result = run_one(env, agent, case, waiting=case != 'INT-06', invalid=case == 'INT-07')
    if case == 'INT-07':
        result['disabled_wait'] = run_one(env, agent, case, waiting=False, invalid=True)
    return result
