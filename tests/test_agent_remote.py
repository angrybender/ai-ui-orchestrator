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
        self.stdin = Buffer()
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
                def readline(self):
                    channel.closed.wait(2)
                    return b""

                def close(self):
                    pass
            return Blocked()
        return Buffer(b"".join(m if isinstance(m, bytes) else (json.dumps(m) + "\n").encode() for m in self.messages))

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
        return self.channel

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
    assert ssh.sftp.files["/tasks/PRJ-12/TASK.md"].decode() == prefix + (
        "# PRJ-12 — Full title\n\nFull description\n$(do not execute)\n\n## Вложения\n\n"
        "- one.txt\n- one-2.txt\n- one-2-2.txt\n")
    assert len(ssh.sftp.files) == 4
    assert ssh.events.index("started") + 1 == ssh.events.index("exec")
    assert ssh.events.index("channel-close") < ssh.events.index("ssh-close")
    assert ssh.sftp.closed
    assert ssh.connect_args["password"] == "secret-password"
    assert ssh.connect_args["port"] == 2222
    assert ssh.connect_args["allow_agent"] is False and ssh.connect_args["look_for_keys"] is False
    command = ssh.channel.command
    import shlex
    assert command == "cd -- /tasks/PRJ-12 && exec agent --acp -p " + shlex.quote(remote.PROMPT)
    assert task["description"] not in command
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "session/new", "session/prompt"]
    assert requests[0]["params"]["clientInfo"]["name"] == "task-orchestrator"
    assert requests[1]["params"] == {"cwd": "/tasks/PRJ-12", "mcpServers": []}
    assert requests[2]["params"]["prompt"][0]["text"] == remote.PROMPT


@pytest.mark.parametrize("resume", [False, True])
def test_acp_shell_without_prompt_placeholder(setup, resume):
    ssh, config, task, _, sessions, run = setup
    config["agent.shell"] = "qwen --acp --yolo --output-format stream-json --model openai/gpt-5.6-luna"
    if resume:
        task["session_id"] = "saved-session"
        task["comment"] = "Continue; $(do not execute) 'quoted'"
        ssh.sftp.dirs.add("/tasks/PRJ-12")
        ssh.channel.messages[1] = reply(2, {})

    result = run()

    assert result["stopReason"] == "end_turn"
    assert ssh.channel.command == "cd -- /tasks/PRJ-12 && exec " + config["agent.shell"]
    requests = [json.loads(line) for line in ssh.channel.stdin.getvalue().splitlines()]
    assert [r["method"] for r in requests] == [
        "initialize", "session/load" if resume else "session/new", "session/prompt"]
    assert requests[2]["params"] == {
        "sessionId": "saved-session" if resume else "session-1",
        "prompt": [{"type": "text", "text": task["comment"] if resume else remote.PROMPT}],
    }
    assert sessions == ["saved-session" if resume else "session-1"]


@pytest.mark.parametrize("task_id", ["", ".", "..", "../outside", "a\\b", "a\x00b", "a\nb", "a\x7fb"])
def test_bad_task_id_before_connect(setup, task_id):
    ssh, _, task, _, _, run = setup
    task["task_id"] = task_id
    with pytest.raises(remote.RemoteAgentError):
        run()
    assert ssh.connect_args is None


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
        ssh.sftp.dirs.add("/tasks/PRJ-12")
        ssh.sftp.files["/tasks/PRJ-12/TASK.md"] = b"preserved"
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
        assert ssh.sftp.files["/tasks/PRJ-12/TASK.md"] == b"preserved"


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
    assert "Нет вложений." in ssh.sftp.files["/tasks/PRJ-12/TASK.md"].decode()


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
    assert "/tasks/PRJ-12/TASK.md" in ssh.sftp.files


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
