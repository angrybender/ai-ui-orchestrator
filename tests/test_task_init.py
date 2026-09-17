"""Init acceptance tests: real Bash channels and isolated SQLite, no external SSH."""
import json
import os
import select
import shlex
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

import agent_remote
import agent_store
import agent_worker
import board_store
from settings import settings as store
from settings.config import Config
from task_cycle import TaskCycle
from task_init import InitError, LIMIT, Output, resolve_script_path, run_init
from test_agent_remote import setup, reply


class Channel:
    """Paramiko's nonblocking channel contract backed by a local subprocess."""
    def __init__(self, group_leader=False):
        self.process = None
        self.closed = False
        self.group_leader = group_leader

    def settimeout(self, timeout):
        pass

    def exec_command(self, command):
        self.process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        **({'process_group': 0} if self.group_leader else {}))

    def ready(self, stream):
        return bool(self.process and select.select([stream], [], [], 0)[0])

    def recv_ready(self):
        return self.process is not None and not self.process.stdout.closed and self.ready(self.process.stdout)

    def recv_stderr_ready(self):
        return self.process is not None and not self.process.stderr.closed and self.ready(self.process.stderr)

    def read(self, stream, size):
        data = os.read(stream.fileno(), size)
        if not data:
            stream.close()
        return data

    def recv(self, size):
        return self.read(self.process.stdout, size)

    def recv_stderr(self, size):
        return self.read(self.process.stderr, size)

    def sendall(self, payload):
        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def shutdown_write(self):
        self.process.stdin.close()

    def exit_status_ready(self):
        return self.process is not None and self.process.poll() is not None

    def recv_exit_status(self):
        return self.process.wait(timeout=5)

    def close(self):
        self.closed = True
        if self.process:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if not stream.closed:
                    stream.close()
            self.process.wait(timeout=5)


class Transport:
    def __init__(self, group_leader=False):
        self.channels = []
        self.group_leader = group_leader

    def open_session(self, timeout):
        channel = Channel(self.group_leader)
        self.channels.append(channel)
        return channel

    def is_active(self):
        return True


@pytest.mark.parametrize('variant', ['text', 'file'])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_init_waits_for_script_when_ssh_command_is_group_leader(tmp_path, variant, exit_code):
    # OpenSSH can start the command as a process-group leader. setsid then
    # forks: its parent's zero status must not replace the script's status.
    script = f'sleep 0.2; printf finished; exit {exit_code}\n'
    path = tmp_path / 'init.sh'
    path.write_text(script)
    config = {'tasks.init_script_text': script} if variant == 'text' else {'tasks.init_script_path': str(path)}
    logs = []
    started = time.monotonic()
    def run():
        run_init(Transport(group_leader=True), None, str(tmp_path), config,
                 sanitize=lambda x: x, active=lambda: True, on_pid=lambda _: None, on_log=logs.append)
    if exit_code:
        with pytest.raises(InitError, match='Init script error: finished'):
            run()
    else:
        run()
    assert time.monotonic() - started >= 0.2
    assert logs == ['Init stdout:\nfinished\n']


def test_zero_exit_without_handshake_reports_startup_failure(tmp_path, monkeypatch):
    execute = Channel.exec_command
    monkeypatch.setattr(Channel, 'exec_command', lambda self, command: execute(self, 'exit 0'))
    with pytest.raises(InitError, match='process exited before startup handshake') as error:
        run_init(Transport(), None, str(tmp_path), {'tasks.init_script_text': 'echo never'},
                 sanitize=lambda x: x, active=lambda: True, on_pid=lambda _: None, on_log=lambda _: None)
    assert not error.value.uncertain


@pytest.mark.parametrize('variant', ['text', 'relative', 'home', 'absolute'])
def test_real_bash_variants_context_and_literal_path(tmp_path, variant):
    cwd = tmp_path / 'task'
    cwd.mkdir()
    requirements = cwd / 'requirements-PRJ-12'
    requirements.mkdir()
    (requirements / 'TASK.md').write_text('context')
    (requirements / 'attachment').write_text('file')
    script = 'test "$(cat requirements-PRJ-12/TASK.md)" = context || exit 9\ntest -f requirements-PRJ-12/attachment || exit 8\nprintf "готово" > result\nprintf warning >&2\n'
    path = tmp_path / "init $(touch INJECTED); script.sh"
    path.write_text(script)
    config = {'tasks.init_script_text': script if variant == 'text' else '',
              'tasks.init_script_path': {'text': '', 'relative': path.name, 'home': '~/' + path.name, 'absolute': str(path)}[variant]}
    logs, pids = [], []
    transport = Transport()
    run_init(transport, SimpleNamespace(normalize=lambda _: str(tmp_path)), str(cwd), config,
             sanitize=agent_remote._Diagnostics([]).safe, active=lambda: True,
             on_pid=pids.append, on_log=logs.append)
    assert (cwd / 'result').read_text() == 'готово'
    assert not (cwd / 'INJECTED').exists()
    assert len(pids) == 1
    assert logs == ['Init stderr:\nwarning\n']
    assert all(c.closed for c in transport.channels)


@pytest.mark.parametrize('script, expected', [('printf out; printf err >&2; exit 3', 'err'),
                                              ('printf out; exit 4', 'out'), ('exit 7', '7')])
def test_error_diagnostic_priority(tmp_path, script, expected):
    with pytest.raises(InitError) as error:
        run_init(Transport(), None, str(tmp_path), {'tasks.init_script_text': script},
                 sanitize=lambda x: x, active=lambda: True, on_pid=lambda _: None, on_log=lambda _: None)
    assert str(error.value) == 'Init script error: ' + expected
    assert not error.value.uncertain


def test_parallel_large_output_and_secret_boundary(tmp_path):
    secret = 'secret-token-value'
    output = Output(agent_remote._Diagnostics([secret]).safe)
    output.append(b'x' * (LIMIT - 5) + b'secre')
    output.append(b't-token-value')
    assert 'secre' not in output.text()
    assert '[REDACTED]' in output.text()
    assert 'truncated' in output.text()
    logs = []
    script = "for i in {1..10000}; do printf 0123456789; printf abcdefghij >&2; done; exit 8"
    with pytest.raises(InitError):
        run_init(Transport(), None, str(tmp_path), {'tasks.init_script_text': script},
                 sanitize=lambda x: x, active=lambda: True, on_pid=lambda _: None, on_log=logs.append)
    assert len(logs) == 2 and all('truncated' in x for x in logs)
    assert all(len(x.encode()) < LIMIT + 100 for x in logs)


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('group_leader', [False, True])
def test_timeout_and_cancel_stop_remote_process(tmp_path, cancel, group_leader):
    started = time.monotonic()
    pids = []
    with pytest.raises(InitError, match='cancelled' if cancel else 'timed out') as error:
        run_init(Transport(group_leader=group_leader), None, str(tmp_path),
                 {'tasks.init_script_text': 'exec sleep 10', 'tasks.init_script_timeout': 1},
                 sanitize=lambda x: x, active=lambda: not cancel or time.monotonic() - started < 0.2,
                 on_pid=pids.append, on_log=lambda _: None)
    assert time.monotonic() - started < 3
    assert not error.value.uncertain
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def section():
    return next(x for x in store.load_sections() if x['key'] == 'tasks')


def test_validation_defaults_preserves_script_and_rejects_conflict(monkeypatch):
    monkeypatch.setattr('paramiko.SSHClient', lambda: pytest.fail('Unexpected SSH'))
    assert store.validate_values(section(), {'base_dir': '/tasks'})['init_script_timeout'] == 300
    text = '  echo hello\n\n'
    value = store.validate_values(section(), {'base_dir': '/tasks', 'init_script_path': '  ', 'init_script_text': text})
    assert value['init_script_path'] == '' and value['init_script_text'] == text
    with pytest.raises(ValueError, match='either path or text'):
        store.validate_values(section(), {'base_dir': '/tasks', 'init_script_path': 'file', 'init_script_text': text})
    for timeout in (0, -1, True, 1.5, 'bad'):
        with pytest.raises(ValueError):
            store.validate_values(section(), {'base_dir': '/tasks', 'init_script_timeout': timeout})


@pytest.mark.parametrize('missing', [False, True])
def test_save_path_checks_regular_file_only(monkeypatch, missing):
    calls = []
    class SSH:
        def __getattr__(self, name):
            return lambda *a, **kw: calls.append((name, a, kw))
        def open_sftp(self):
            return self
        def get_channel(self):
            return self
        def normalize(self, value):
            return '/home/test'
        def stat(self, path):
            calls.append(('stat', path))
            if missing:
                raise FileNotFoundError
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600)
    monkeypatch.setattr('paramiko.SSHClient', SSH)
    monkeypatch.setattr(Config, 'snapshot', lambda _: {'remote_server.host': 'host', 'remote_server.username': 'test'})
    values = {'base_dir': '/tasks', 'init_script_path': '  ~/file $literal.sh  '}
    if missing:
        with pytest.raises(ValueError, match='file not found'):
            store.validate_values(section(), values)
    else:
        assert store.validate_values(section(), values)['init_script_path'] == '~/file $literal.sh'
    assert ('stat', '/home/test/file $literal.sh') in calls
    assert not any(x[0] == 'exec_command' for x in calls)


def test_full_cycle_retry_then_reopen_preserves_context_and_history(setup, monkeypatch):
    ssh, config, task, _, _, _ = setup
    config.update({'tasks.init_script_text': 'exit 2', 'tasks.init_script_timeout': 300, 'agent.continue_session_arg': '--continue'})
    monkeypatch.setattr(Config, 'get', staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task(task['task_id'], task['title'], task['description'], [])
    board_store.move_task(task['task_id'], 'OPEN', 0)
    calls = []
    def init(transport, sftp, cwd, snapshot, **kwargs):
        calls.append(snapshot['tasks.init_script_text'])
        assert sftp.files[cwd + '/requirements-PRJ-12/TASK.md']
        assert board_store.get_task(task['task_id'])['phase'] == 'Init'
        assert agent_store.claim(60) is None
        if len(calls) == 1:
            raise InitError('Init script error: failure')
    monkeypatch.setattr(agent_remote, 'run_init', init)
    assert agent_worker.execute() == 1
    assert ssh.channel.command is None
    initial = board_store.get_chat(task['task_id'])
    original_files = dict(ssh.sftp.files)
    assert initial['status'] == 'WAIT'
    assert initial['messages'][0]['role'] == 'system'
    assert initial['messages'][0]['run_id']
    config['tasks.init_script_text'] = 'echo fixed'
    board_store.add_user_comment(task['task_id'], 'Please retry')
    assert agent_worker.execute() == 0
    assert ssh.sftp.files == original_files
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert requests[1]['method'] == 'session/new'
    assert 'TASK.md' in requests[2]['params']['prompt'][0]['text']
    assert 'Please retry' in requests[2]['params']['prompt'][0]['text']
    assert '--continue' not in ssh.channel.command
    assert board_store.get_chat(task['task_id'])['messages'][0] == initial['messages'][0]
    assert calls == ['exit 2', 'echo fixed']
    board_store.move_task(task['task_id'], 'OPEN', 0)
    ssh.channel.messages[1] = reply(2, {})  # session/load response
    assert agent_worker.execute() == 0
    assert calls == ['exit 2', 'echo fixed']
    assert ssh.channel.command.endswith('--continue')


def test_cancel_fences_late_success_and_uncertain_retry():
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('T-1', 'Task', 'Description', [])
    board_store.move_task('T-1', 'OPEN', 0)
    run = agent_store.claim(1)
    cycle = TaskCycle(run, {'tasks.init_script_timeout': 300, 'agent.agent_timeout': 1})
    cycle.phase('Init')
    assert agent_store.active_run()['timeout'] == 300
    board_store.move_task('T-1', 'BACKLOG', 0)
    assert not cycle.active()
    agent_store.finish(run['id'], 'Init script error: cancelled', init_error=True, uncertain=True)
    assert board_store.get_task('T-1')['status'] == 'BACKLOG'
    board_store.move_task('T-1', 'OPEN', 0)
    assert agent_store.claim(1) is None
    agent_store.finish(run['id'])
    assert board_store.get_task('T-1')['status'] == 'OPEN'


def test_cancel_stops_descendants(tmp_path):
    started = time.monotonic()
    with pytest.raises(InitError) as error:
        run_init(Transport(), None, str(tmp_path),
                 {'tasks.init_script_text': 'sleep 10 &\necho $! > child\nwait'},
                 sanitize=lambda x: x, active=lambda: time.monotonic() - started < 0.3,
                 on_pid=lambda _: None, on_log=lambda _: None)
    assert not error.value.uncertain
    pid = int((tmp_path / 'child').read_text())
    try:
        from pathlib import Path
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return
    assert state == 'Z'


def test_unconfirmed_stop_is_persisted_and_no_agent_starts(setup, monkeypatch):
    ssh, config, task, _, _, _ = setup
    config['tasks.init_script_text'] = 'sleep 10'
    monkeypatch.setattr(Config, 'get', staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task(task['task_id'], task['title'], task['description'], [])
    board_store.move_task(task['task_id'], 'OPEN', 0)
    def fail(*args, **kwargs):
        raise InitError('Init script error: SSH connection lost', uncertain=True)
    monkeypatch.setattr(agent_remote, 'run_init', fail)
    assert agent_worker.execute() == 1
    assert ssh.channel.command is None
    board_store.add_user_comment(task['task_id'], 'Retry')
    assert agent_store.claim(60) is None
    assert 'retry blocked' in board_store.get_chat(task['task_id'])['messages'][0]['text']


def test_init_success_survives_agent_failure(setup, monkeypatch):
    ssh, config, task, _, _, _ = setup
    config['tasks.init_script_text'] = 'echo ok'
    monkeypatch.setattr(Config, 'get', staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task(task['task_id'], task['title'], task['description'], [])
    board_store.move_task(task['task_id'], 'OPEN', 0)
    calls = []
    monkeypatch.setattr(agent_remote, 'run_init', lambda *a, **kw: calls.append(1))
    ssh.channel.messages[0] = b'bad json\n'
    assert agent_worker.execute() == 1
    board_store.add_user_comment(task['task_id'], 'Retry the agent')
    ssh.channel.messages[0] = reply(1, {'protocolVersion': 1})
    assert agent_worker.execute() == 0
    assert calls == [1]
    assert all(x['role'] != 'system' for x in board_store.get_chat(task['task_id'])['messages'])


@pytest.mark.parametrize('requirements_exist', [False, True])
def test_unknown_existing_directory_stays_rejected(setup, monkeypatch, requirements_exist):
    ssh, config, task, _, _, _ = setup
    config['tasks.init_script_text'] = 'echo ok'
    monkeypatch.setattr(Config, 'get', staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task(task['task_id'], task['title'], task['description'], [])
    board_store.move_task(task['task_id'], 'OPEN', 0)
    ssh.sftp.dirs.add('/tasks/PRJ-12')
    name = 'requirements-PRJ-12' if requirements_exist else 'PRJ-12'
    if requirements_exist:
        ssh.sftp.dirs.add('/tasks/PRJ-12/requirements-PRJ-12')
    monkeypatch.setattr(agent_remote, 'run_init', lambda *a, **kw: pytest.fail('Unknown context reused'))
    assert agent_worker.execute() == 1
    chat = board_store.get_chat(task['task_id'])
    assert chat['status'] == 'WAIT'
    assert chat['messages'][0]['text'] == f'Task directory {name} already exists.'
    assert board_store.get_task(task['task_id'])['is_error']
    board_store.add_user_comment(task['task_id'], 'Retry')
    assert agent_worker.execute() == 1
    assert ssh.channel.command is None


def test_settings_snapshot_is_consistent_under_concurrent_save(tmp_path):
    import concurrent.futures
    keys = ['tasks.init_script_path', 'tasks.init_script_text']
    def save(index):
        store.save_values('tasks', {'init_script_path': 'file' if index % 2 else '',
                                   'init_script_text': '' if index % 2 else 'text'})
    save(0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(lambda: [save(i) for i in range(30)])
        for _ in range(30):
            snapshot = Config.snapshot(keys)
            assert bool(snapshot[keys[0]]) != bool(snapshot[keys[1]])
        future.result()


def test_base_directory_contains_task_but_is_not_init_cwd(setup, monkeypatch):
    ssh, config, task, _, _, run = setup
    config['tasks.base_dir'] = '/remote/projects'
    config['tasks.init_script_text'] = 'pwd'
    init_cwds = []
    def init(transport, sftp, cwd, config, **callbacks):
        assert cwd + '/requirements-PRJ-12/TASK.md' in sftp.files
        init_cwds.append(cwd)
    monkeypatch.setattr(agent_remote, 'run_init', init)
    run()
    assert init_cwds == ['/remote/projects/PRJ-12']
    assert ssh.channel.command.startswith('cd -- /remote/projects/PRJ-12 && exec ')
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert requests[1]['params']['cwd'] == init_cwds[0]


def test_base_directory_is_editable_and_required():
    store.save_values('tasks', {'base_dir': '/existing/tasks'})
    normalized = store.validate_values(section(), {'base_dir': '  /new/tasks  ', 'init_script_text': 'pwd'})
    store.save_values('tasks', normalized)
    assert Config.get('tasks.base_dir') == '/new/tasks'
    assert Config.get('tasks.init_script_text') == 'pwd'
    with pytest.raises(ValueError, match='base_dir'):
        store.validate_values(section(), {'init_script_text': 'pwd'})


def test_missing_base_directory_rejected_before_ssh(setup):
    ssh, config, task, _, _, run = setup
    config.pop('tasks.base_dir')
    with pytest.raises(agent_remote.RemoteAgentError):
        run()
    assert ssh.connect_args is None
