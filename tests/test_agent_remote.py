"""Transport tests: only in-memory fake SSH, no saved config or network."""
import io
import json
import threading
import time

import pytest

import agent_remote as remote


def reply(number, result):
    return {"jsonrpc": "2.0", "id": number, "result": result}


def update(text):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}}


class Buffer(io.BytesIO):
    def close(self):
        self.was_closed = True


class FakeChannel:
    def __init__(self, owner):
        self.owner = owner
        class Input(Buffer):
            def write(inner, data):
                if data == b'GO\n':
                    owner.events.append('gate-open')
                    return len(data)
                return super().write(data)
        self.stdin = Input()
        self.closed = threading.Event()
        self.block_exec = False
        self.block_stdout = False
        self.block_stderr = False
        self.exit_code = None
        self.stderr_chunks = []
        self.command = None
        self.messages = [reply(1, {"protocolVersion": 1}), reply(2, {"sessionId": "session-1"}),
                         reply(3, {"stopReason": "end_turn"})]

    def exec_command(self, command):
        self.owner.events.append("exec")
        self.command = command
        if self.block_exec:
            self.closed.wait(2)
            raise OSError("secret-password")

    def makefile_stdin(self, mode):
        assert mode == "wb"
        return self.stdin

    def makefile(self, mode):
        assert mode == "rb"
        if self.block_stdout:
            channel = self

            class Blocked:
                def readline(self, size=-1):
                    channel.closed.wait(2)
                    return b""

                def close(self):
                    pass
            return Blocked()
        return Buffer(b'ACP_PID:12345\n' + b"".join(m if isinstance(m, bytes) else (json.dumps(m) + "\n").encode() for m in self.messages))

    def recv_stderr(self, size):
        if self.stderr_chunks:
            return self.stderr_chunks.pop(0)
        if self.block_stderr:
            self.closed.wait(2)
        return b""

    def exit_status_ready(self):
        return self.exit_code is not None

    def recv_exit_status(self):
        assert self.exit_status_ready(), "must not wait for persistent ACP process"
        return self.exit_code

    def close(self):
        self.owner.events.append("channel-close")
        self.closed.set()


class FakeSFTP:
    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.closed = False
        self.fail_upload = False
        self.escape = False

    def normalize(self, path):
        return "/escaped" if self.escape and path.endswith("PRJ-12") else path

    def mkdir(self, path):
        if path in self.dirs:
            raise OSError("secret-password existing directory")
        self.dirs.add(path)

    def lstat(self, path):
        import stat
        from types import SimpleNamespace
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)

    def putfo(self, source, path):
        assert isinstance(source, io.BytesIO)
        if self.fail_upload:
            raise OSError("secret-password")
        assert path not in self.files
        self.files[path] = source.read()

    def put(self, local, path):
        assert path not in self.files
        self.files[path] = local

    def close(self):
        self.closed = True


class FakeSSH:
    def __init__(self):
        self.events = []
        self.channel = FakeChannel(self)
        self.sftp = FakeSFTP()
        self.connect_args = None

    def load_system_host_keys(self):
        self.events.append("host-keys")

    def set_missing_host_key_policy(self, policy):
        assert isinstance(policy, remote.paramiko.RejectPolicy)
        self.events.append("reject-policy")

    def connect(self, **kwargs):
        self.connect_args = kwargs

    def get_transport(self):
        return self

    def set_keepalive(self, seconds):
        assert seconds > 0
        self.events.append("keepalive")

    def open_session(self, timeout):
        assert timeout > 0
        if self.channel.command is not None and not self.channel.closed.is_set():
            owner = self
            class Control:
                def settimeout(self, timeout):
                    pass
                def exec_command(self, command):
                    assert 'kill -TERM -- -12345' in command
                    owner.events.append('stop-group')
                def exit_status_ready(self):
                    return True
                def recv_exit_status(self):
                    return 0
                def close(self):
                    owner.events.append('control-close')
            return Control()
        self.channel.closed.clear()
        return self.channel

    def is_active(self):
        return True

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.events.append("ssh-close")


@pytest.fixture
def setup(monkeypatch):
    ssh = FakeSSH()
    monkeypatch.setattr(remote.paramiko, "SSHClient", lambda: ssh)
    config = {"remote_server.host": "example.test:2222", "remote_server.username": "test-user",
              "remote_server.password": "secret-password", "remote_server.ssh_key": "",
              "tasks.base_dir": "/tasks/./", "agent.shell": "agent --acp -p ${PROMPT}",
              "agent.agent_timeout": 1}
    task = {"task_id": "PRJ-12", "title": "Full title", "description": "Full description\n$(do not execute)"}
    logs, sessions = [], []

    def run(attachments=()):
        return remote.run_remote(task, list(attachments), config, sessions.append, logs.append,
                                 lambda: ssh.events.append("started"))
    return ssh, config, task, logs, sessions, run


@pytest.mark.parametrize("system_prompt", [None, "", "Системные инструкции\n  Сохрани отступы и $(literal).\n"])
def test_success_context_auth_and_protocol(setup, tmp_path, system_prompt):
    ssh, config, task, logs, sessions, run = setup
    if system_prompt is not None:
        config["agent.prompt"] = system_prompt
    file = tmp_path / "attachment"
    file.write_bytes(b"bytes")
    result = run([(file, "C:\\folder\\one.txt"), (file, "../one.txt"), (file, "one-2.txt")])
    assert result == {"sessionId": "session-1", "stopReason": "end_turn"}
    assert sessions == ["session-1"]
    prefix = f"{system_prompt}\n\n" if system_prompt else ""
    assert ssh.sftp.files["/tasks/PRJ-12/requirements-PRJ-12/TASK.md"].decode() == prefix + (
        "# PRJ-12 — Full title\n\nFull description\n$(do not execute)\n\n## Вложения\n\n"
        "- one.txt\n- one-2.txt\n- one-2-2.txt\n")
    assert len(ssh.sftp.files) == 4
    assert set(ssh.sftp.files) == {
        '/tasks/PRJ-12/requirements-PRJ-12/' + name
        for name in ('TASK.md', 'one.txt', 'one-2.txt', 'one-2-2.txt')}
    assert ssh.events.index("started") + 1 == ssh.events.index("exec")
    assert ssh.events.index("channel-close") < ssh.events.index("ssh-close")
    assert ssh.sftp.closed
    assert ssh.connect_args["password"] == "secret-password"
    assert ssh.connect_args["port"] == 2222
    assert ssh.connect_args["allow_agent"] is False and ssh.connect_args["look_for_keys"] is False
    command = ssh.channel.command
    import shlex
    supervisor = shlex.split(command.split('bash -c ', 1)[1])[0]
    assert supervisor.endswith("exec agent --acp -p " + shlex.quote(remote.task_prompt(task["task_id"])))
    assert 'setsid --wait' in command
    assert ssh.events.index('stop-group') < ssh.events.index('channel-close')
    assert task["description"] not in command
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "session/new", "session/prompt"]
    assert requests[0]["params"]["clientInfo"]["name"] == "task-orchestrator"
    assert requests[1]["params"] == {"cwd": "/tasks/PRJ-12", "mcpServers": []}
    assert requests[2]["params"]["prompt"][0]["text"] == remote.task_prompt(task["task_id"])


@pytest.mark.parametrize("resume", [False, True])
def test_acp_shell_without_prompt_placeholder(setup, resume):
    ssh, config, task, _, sessions, run = setup
    config["agent.shell"] = "qwen --acp --yolo --output-format stream-json --model openai/gpt-5.6-luna"
    if resume:
        task["session_id"] = "saved-session"
        task["comment"] = "Continue; $(do not execute) 'quoted'"
        ssh.sftp.dirs.update({"/tasks/PRJ-12", "/tasks/PRJ-12/requirements-PRJ-12"})
        ssh.channel.messages[1] = reply(2, {})

    result = run()

    assert result["stopReason"] == "end_turn"
    import shlex
    assert shlex.split(ssh.channel.command.split('bash -c ', 1)[1])[0].endswith('exec ' + config['agent.shell'])
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r["method"] for r in requests] == [
        "initialize", "session/load" if resume else "session/new", "session/prompt"]
    assert requests[2]["params"] == {
        "sessionId": "saved-session" if resume else "session-1",
        "prompt": [{"type": "text", "text": task["comment"] if resume else remote.task_prompt(task["task_id"])}],
    }
    assert sessions == ["saved-session" if resume else "session-1"]


@pytest.mark.parametrize("task_id", ["", ".", "..", "../outside", "a\\b", "a\x00b", "a\nb", "a\x7fb"])
def test_bad_task_id_before_connect(setup, task_id):
    ssh, _, task, _, _, run = setup
    task["task_id"] = task_id
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert ssh.connect_args is None


@pytest.mark.parametrize("diagnostic", [
    "Session not found: saved-session", "Session saved-session not found",
    "Session does not exist", "Unknown session: saved-session",
    "Session 'saved-session' does not exist",
])
@pytest.mark.parametrize("comment", [None, "Continue with the requested fix"])
def test_missing_session_creates_new_session(setup, diagnostic, comment):
    ssh, config, task, logs, sessions, run = setup
    task.update(session_id="saved-session", comment=comment)
    config['agent.continue_session_arg'] = '--continue'
    ssh.sftp.dirs.update({"/tasks/PRJ-12", "/tasks/PRJ-12/requirements-PRJ-12"})
    ssh.sftp.files['/tasks/PRJ-12/requirements-PRJ-12/TASK.md'] = b'original context'
    ssh.channel.messages = [reply(1, {"protocolVersion": 1}),
        {"jsonrpc": "2.0", "id": 2, "error": {
            "code": -32603, "message": "Internal error", "data": {"details": diagnostic}}},
        reply(3, {"sessionId": "replacement-session"}), reply(4, {"stopReason": "end_turn"})]
    assert run() == {"sessionId": "replacement-session", "stopReason": "end_turn"}
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r['method'] for r in requests] == ['initialize', 'session/load', 'session/new', 'session/prompt']
    assert [r['id'] for r in requests] == [1, 2, 3, 4]
    expected = remote.task_prompt(task['task_id']) + ('\n\n' + comment if comment else '')
    assert requests[-1]['params'] == {'sessionId': 'replacement-session', 'prompt': [{'type': 'text', 'text': expected}]}
    assert sessions == ['replacement-session']
    assert ssh.sftp.files == {'/tasks/PRJ-12/requirements-PRJ-12/TASK.md': b'original context'}
    assert 'creating a new session' in logs[0]
    assert ssh.events.count('exec') == 1


@pytest.mark.parametrize('message,response_id', [
    ('Permission denied', 2), ('Session temporarily unavailable', 2),
    ('Method not found', 2), ('Session file not found: configuration.json', 2),
    ('Session not found', 99),
])
def test_load_errors_do_not_create_replacement(setup, message, response_id):
    ssh, _, task, _, sessions, run = setup
    task['session_id'] = 'saved-session'
    ssh.sftp.dirs.update({'/tasks/PRJ-12', '/tasks/PRJ-12/requirements-PRJ-12'})
    ssh.channel.messages[1] = {'jsonrpc': '2.0', 'id': response_id,
                               'error': {'code': -32603, 'message': message}}
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert sessions == []
    assert [json.loads(line)['method'] for line in ssh.channel.stdin.getvalue().splitlines()] == ['initialize', 'session/load']


@pytest.mark.parametrize('replacement', [
    reply(3, {'sessionId': ''}),
    {'jsonrpc': '2.0', 'id': 3, 'error': {'code': -32603, 'message': 'Session not found'}},
])
def test_failed_replacement_is_not_retried(setup, replacement):
    ssh, _, task, _, sessions, run = setup
    task['session_id'] = 'saved-session'
    ssh.sftp.dirs.update({'/tasks/PRJ-12', '/tasks/PRJ-12/requirements-PRJ-12'})
    ssh.channel.messages[1:] = [
        {'jsonrpc': '2.0', 'id': 2, 'error': {'code': -32603, 'message': 'Session not found'}}, replacement]
    with pytest.raises(remote.RemoteAgentError, match='session/new'):
        run()
    assert sessions == []
    assert len(ssh.channel.stdin.getvalue().splitlines()) == 3


def test_backlog_edits_then_missing_session_recovery_persisted(setup, monkeypatch):
    import agent_store
    import agent_worker
    import board_store

    ssh, config, _, _, _, _ = setup
    monkeypatch.setattr(agent_worker.Config, 'get', staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task('PRJ-12', 'Initial', 'Initial description', [])
    for n in range(4):
        board_store.update_task('PRJ-12', f'Title {n}', f'Description {n}', 'BACKLOG', [], [])
    with board_store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 0
    board_store.move_task('PRJ-12', 'OPEN', 0)
    assert agent_worker.execute() == 0
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r['method'] for r in requests] == ['initialize', 'session/new', 'session/prompt']
    original_files = dict(ssh.sftp.files)
    assert b'Description 3' in next(iter(original_files.values()))

    board_store.add_user_comment('PRJ-12', 'Continue the work')
    ssh.channel = FakeChannel(ssh)
    ssh.channel.messages = [reply(1, {'protocolVersion': 1}),
        {'jsonrpc': '2.0', 'id': 2, 'error': {'code': -32603, 'message': 'Session not found'}},
        reply(3, {'sessionId': 'replacement-session'}), update('Completed after recovery'),
        reply(4, {'stopReason': 'end_turn'})]
    original_session = agent_store.session

    def persist(run_id, session_id):
        task = board_store.get_task('PRJ-12')
        assert task['status'] == 'IN PROGRESS' and not task['is_error']
        original_session(run_id, session_id)

    monkeypatch.setattr(agent_store, 'session', persist)
    assert agent_worker.execute() == 0
    task = board_store.get_task('PRJ-12')
    assert task['status'] == 'REVIEW' and not task['is_error']
    assert ssh.sftp.files == original_files
    with board_store.connect() as db:
        runs = db.execute('SELECT * FROM agent_runs ORDER BY created_at').fetchall()
    assert len(runs) == 2
    assert runs[-1]['session_id'] == 'replacement-session'
    assert runs[-1]['state'] == 'SUCCEEDED' and not runs[-1]['error']
    assert [m['text'] for m in board_store.get_chat('PRJ-12')['messages']] == ['Continue the work', 'Completed after recovery']
    board_store.add_user_comment('PRJ-12', 'Verify once more')
    next_run = agent_store.claim(60)
    assert next_run['session_id'] == 'replacement-session'
    agent_store.finish(next_run['id'], stop_reason='end_turn')


@pytest.mark.parametrize("key,value", [("remote_server.host", "host:99999"), ("remote_server.username", ""),
    ("tasks.base_dir", "relative"), ("agent.shell", ""), ("agent.agent_timeout", 0),
    ("agent.agent_timeout", float("inf")), ("remote_server.password", "")])
def test_invalid_configuration_before_connect(setup, key, value):
    ssh, config, _, _, _, run = setup
    config[key] = value
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert ssh.connect_args is None


@pytest.mark.parametrize("name", ["", ".", "..", "TASK.md", "task.MD", "x/TASK.md", "a/", "a\\", "a\nb", "a\x00b"])
def test_invalid_attachment_before_connect(setup, tmp_path, name):
    ssh, _, _, _, _, run = setup
    local = tmp_path / "file"
    local.write_text("test")
    with pytest.raises(remote.RemoteAgentError):
        run([(local, name)])
    assert ssh.connect_args is None


@pytest.mark.parametrize("failure", ["existing", "upload", "normalize"])
def test_preparation_failure_never_executes(setup, failure):
    ssh, _, _, _, _, run = setup
    if failure == "existing":
        ssh.sftp.dirs.update({"/tasks/PRJ-12", "/tasks/PRJ-12/requirements-PRJ-12"})
        ssh.sftp.files["/tasks/PRJ-12/requirements-PRJ-12/TASK.md"] = b"preserved"
    elif failure == "upload":
        ssh.sftp.fail_upload = True
    else:
        ssh.sftp.escape = True
    with pytest.raises(remote.RemoteAgentError) as error:
        run()
    assert "secret-password" not in str(error.value)
    assert "exec" not in ssh.events
    assert ssh.sftp.closed
    if failure == "existing":
        assert ssh.sftp.files["/tasks/PRJ-12/requirements-PRJ-12/TASK.md"] == b"preserved"
        assert str(error.value) == 'Task directory requirements-PRJ-12 already exists.'


@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.parametrize('unsafe', ['symlink', 'escape'])
def test_unsafe_requirements_directory_never_starts(setup, monkeypatch, reuse, unsafe):
    import stat
    from types import SimpleNamespace
    ssh, _, task, _, _, run = setup
    directory = '/tasks/PRJ-12/requirements-PRJ-12'
    if reuse:
        task['session_id'] = 'saved-session'
        ssh.sftp.dirs.update({'/tasks/PRJ-12', directory})
    if unsafe == 'symlink':
        original = ssh.sftp.lstat
        monkeypatch.setattr(ssh.sftp, 'lstat', lambda path: SimpleNamespace(st_mode=stat.S_IFLNK)
                            if path == directory else original(path))
        if not reuse:
            ssh.sftp.dirs.add(directory)  # mkdir collides with a pre-existing symlink.
    else:
        monkeypatch.setattr(ssh.sftp, 'normalize', lambda path: '/outside' if path == directory else path)
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert not ssh.sftp.files
    assert ssh.channel.command is None


def test_partial_context_cannot_be_overwritten_on_retry(setup, tmp_path, monkeypatch):
    ssh, _, _, _, _, run = setup
    local = tmp_path / 'file'
    local.write_text('attachment')
    monkeypatch.setattr(ssh.sftp, 'put', lambda *args: (_ for _ in ()).throw(OSError('upload failed')))
    with pytest.raises(remote.RemoteAgentError, match='upload failed'):
        run([(local, 'file.txt')])
    saved = dict(ssh.sftp.files)
    with pytest.raises(remote.RemoteAgentError, match='Task directory requirements-PRJ-12 already exists.'):
        run([(local, 'file.txt')])
    assert ssh.sftp.files == saved
    assert ssh.channel.command is None


@pytest.mark.parametrize("bad", [b"not-json\n", b"\xff\n", b"\n", b'{}', [],
    {"jsonrpc": "2.0", "id": 99, "result": {}}, {"jsonrpc": "2.0", "id": True, "result": {}},
    {"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 1, "error": "secret-password"},
    reply(1, {"protocolVersion": True}), reply(1, {"protocolVersion": 2}),
    b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n'])
def test_strict_acp_failures(setup, bad):
    ssh, _, _, logs, _, run = setup
    ssh.channel.messages = [update("available diagnostic"), bad]
    with pytest.raises(remote.RemoteAgentError) as error:
        run()
    assert "initialize:" in str(error.value)
    assert logs[0].startswith("available diagnostic\n")
    assert "initialize:" in logs[0]
    assert ssh.channel.closed.is_set()


def test_requests_events_and_secret_chunk_redaction(setup, tmp_path):
    ssh, config, _, logs, _, run = setup
    key = tmp_path / "private.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nABCDEFsecretmaterial123456\n-----END PRIVATE KEY-----\n")
    config["remote_server.ssh_key"] = str(key)
    messages = [update("helpful: "), update("secret-"), update("password"), update(str(key)[:8]),
                update(str(key)[8:]), update("-----BE"), update("GIN PRIVATE KEY-----\n"),
                update("unconfigured body"), update("\n-----END PRIVATE KEY-----"),
                {"jsonrpc": "2.0", "method": "dangerous", "id": "remote-request", "params": {}},
                {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                    "sessionUpdate": "plan", "entries": [{"content": "plan entry"}]}}},
                {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                    "sessionUpdate": "tool_call", "title": "Shell: pytest"}}}]
    ssh.channel.messages[2:2] = messages
    ssh.channel.stderr_chunks = [b"stderr diagnostic secret-", b"password ABCDEFsecret", b"material123456"]
    run()
    assert ssh.connect_args["key_filename"] == str(key)
    assert "password" not in ssh.connect_args
    assert "ABCDEFsecretmaterial123456" not in logs[0]
    assert str(key) not in logs[0]
    assert len(logs) == 1
    assert "helpful" in logs[0] and "plan entry" in logs[0] and "Shell: pytest" in logs[0]
    assert "secret-password" not in logs[0]
    assert "unconfigured body" not in logs[0]
    responses = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert responses[-1]["error"] == {"code": -32601, "message": "Method not supported"}


@pytest.mark.parametrize("stage", ["exec", "stdout", "stderr"])
def test_timeout_and_bounded_cleanup(setup, stage):
    ssh, config, _, _, _, run = setup
    config["agent.agent_timeout"] = 0.04
    setattr(ssh.channel, "block_" + stage, True)
    start = time.monotonic()
    if stage == "stderr":
        assert run()["stopReason"] == "end_turn"
    else:
        with pytest.raises(remote.RemoteAgentError, match="timed out"):
            run()
    assert time.monotonic() - start < 0.7
    assert ssh.events.index("channel-close") < ssh.events.index("ssh-close")


@pytest.mark.parametrize("reason", ["cancelled", "error", "failed"])
def test_failure_stop_reason(setup, reason):
    ssh, _, _, _, sessions, run = setup
    ssh.channel.messages[-1] = reply(3, {"stopReason": reason})
    with pytest.raises(remote.RemoteAgentError) as error:
        run()
    assert error.value.stop_reason == reason
    assert sessions == ["session-1"]


def test_exit_status_failure(setup):
    ssh, _, _, _, _, run = setup
    ssh.channel.exit_code = 12
    with pytest.raises(remote.RemoteAgentError):
        run()


def test_internal_failure_preserves_safe_diagnostic(setup):
    ssh, _, _, logs, _, run = setup
    ssh.channel.messages = [reply(1, {"protocolVersion": 1}), reply(2, {"sessionId": []})]
    with pytest.raises(remote.RemoteAgentError, match=r"ValueError"):
        run()
    assert logs and "session/new: ValueError: Missing, invalid or unsafe sessionId" in logs[0]


def test_secret_session_id_rejected_without_callback(setup):
    ssh, _, _, _, sessions, run = setup
    ssh.channel.messages[1] = reply(2, {"sessionId": "secret-password"})
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert sessions == []


def test_empty_attachments_and_default_port(setup):
    ssh, config, _, _, _, run = setup
    config["remote_server.host"] = "host"
    run()
    assert ssh.connect_args["port"] == 22
    assert "Нет вложений." in ssh.sftp.files["/tasks/PRJ-12/requirements-PRJ-12/TASK.md"].decode()


def test_exec_timeout_preserves_redacted_stderr(setup):
    ssh, config, _, logs, _, run = setup
    config["agent.agent_timeout"] = 0.04
    ssh.channel.block_exec = True
    ssh.channel.stderr_chunks = [b"available secret-", b"password"]
    with pytest.raises(remote.RemoteAgentError, match="timed out"):
        run()
    assert logs == ["available [REDACTED]"]


def test_partial_secret_and_unfinished_pem_on_failure(setup):
    ssh, _, _, logs, _, run = setup
    ssh.channel.messages = [update("useful secret-pass"), b"invalid\n"]
    ssh.channel.stderr_chunks = [b"diagnostic -----BEGIN PRIVATE KEY-----\nbody-not-in-config"]
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert "secret-pass" not in logs[0]
    assert "body-not-in-config" not in logs[0]
    assert "useful" in logs[0] and "diagnostic" in logs[0]


def test_attachment_upload_failure_does_not_start(setup, tmp_path, monkeypatch):
    ssh, _, _, _, _, run = setup
    local = tmp_path / "file"
    local.write_text("data")

    def fail(*args):
        raise OSError("secret-password")

    monkeypatch.setattr(ssh.sftp, "put", fail)
    with pytest.raises(remote.RemoteAgentError):
        run([(local, "file.txt")])
    assert ssh.channel.command is None
    assert "/tasks/PRJ-12/requirements-PRJ-12/TASK.md" in ssh.sftp.files


@pytest.mark.parametrize("stderr", [b"", b"Internal error: Connection error.\n"])
def test_worker_persists_acp_error_in_log_status_and_chat(setup, monkeypatch, stderr):
    import agent_store
    import agent_worker
    import board_store

    ssh, config, task, _, _, _ = setup
    raw = Buffer()
    monkeypatch.setattr(remote, "_open_raw_log", lambda _: raw)
    monkeypatch.setattr(agent_worker.Config, "get", staticmethod(config.get))
    board_store.init_database()
    agent_store.init_database()
    board_store.create_task(task["task_id"], task["title"], task["description"], [])
    board_store.move_task(task["task_id"], "OPEN", 0)
    ssh.channel.messages[2:] = [update("Agent completed context"), {
        "jsonrpc": "2.0", "id": 3, "error": {
            "code": -32603, "message": "Internal error",
            "data": {"details": "Connection error."}}}]
    ssh.channel.stderr_chunks = [stderr] if stderr else []
    assert agent_worker.execute() == 1
    with board_store.connect() as connection:
        run = dict(connection.execute("SELECT * FROM agent_runs").fetchone())
    assert run["state"] == "FAILED"
    assert "Connection error." in run["error"]
    assert "Connection error." in run["log"]
    chat = board_store.get_chat(task["task_id"])
    assert chat["status"] == "WAIT"
    assert board_store.get_task(task["task_id"])["is_error"] is True
    assert "Agent completed context" in chat["messages"][0]["text"]
    assert "Connection error." in chat["messages"][0]["text"]
    assert b'"details": "Connection error."' in raw.getvalue()
    if stderr:
        assert b"ERR " + stderr in raw.getvalue()
