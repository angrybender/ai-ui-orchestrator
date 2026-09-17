"""Executor integration tests: real OS processes and SQLite, no SSH connections."""
from __future__ import annotations

import importlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import agent_store
import board_store


CONFIG = {
    "remote_server.username": "tester",
    "remote_server.host": "example.invalid:2222",
    "remote_server.password": "private-password-do-not-log",
    "remote_server.ssh_key": "",
    "tasks.base_dir": "/remote/tasks",
    "agent.shell": "fake-agent --prompt ${PROMPT}",
    "agent.agent_timeout": 60,
    "agent.prompt": "Системные инструкции\nВыполни проверки.",
    "agent.continue_session_arg": "--continue",
    "tasks.init_script_path": "", "tasks.init_script_text": "", "tasks.init_script_timeout": 300,
}


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(board_store, "DATABASE_PATH", tmp_path / "board.db")
    monkeypatch.setattr(board_store, "FILES_DIR", tmp_path / "files")
    board_store.init_database()
    agent_store.init_database()
    return board_store.DATABASE_PATH, board_store.FILES_DIR


@pytest.fixture
def worker(database, monkeypatch):
    module = importlib.import_module("agent_worker")
    monkeypatch.setattr(module.Config, "get", staticmethod(CONFIG.__getitem__))
    return module


def _seed_tasks():
    # Insertion order and sort_order deliberately disagree; the first two
    # candidates tie, so internal id (not task_id) must break the tie.
    for task_id, order in (("PRJ-10", 8), ("PRJ-9", 1), ("PRJ-1", 1)):
        board_store.create_task(task_id, "Полное название", "Описание\nбез сокращений", [])
        with board_store.connect() as connection:
            connection.execute(
                "UPDATE board_tasks SET status = 'OPEN', sort_order = ?, is_error = 1 WHERE task_id = ?",
                (order, task_id),
            )
    return "PRJ-9"


def _runs():
    with board_store.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs ORDER BY created_at, id")]


def _snapshot():
    with board_store.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("board_tasks", "agent_runs")
        }


def _concurrent_worker(database, files, barrier, release, sessions, results, outcome, connections):
    import agent_worker

    board_store.DATABASE_PATH = Path(database)
    board_store.FILES_DIR = Path(files)
    agent_worker.settings_store.USER_SETTINGS_DIR = Path(database).parent / "settings"
    agent_worker.Config.get = staticmethod(CONFIG.__getitem__)

    def transport(task, attachments, config, on_session, on_log, on_started, on_message):
        on_started()
        on_session("session-one")
        on_log("План: выполнить задачу\n")
        sessions.put((os.getpid(), task["task_id"]))
        if not release.wait(20):
            raise AssertionError("Parent did not release fake ACP response")
        if outcome == "failure":
            raise agent_worker.RemoteAgentError("ACP failed safely")
        if outcome == "timeout":
            raise TimeoutError("Fake remote deadline expired")
        return {"stopReason": "end_turn"}

    if outcome.startswith("ssh-"):
        import io
        import json
        import threading
        import agent_remote

        remote_outcome = outcome.removeprefix("ssh-")
        if remote_outcome == "timeout":
            config = {**CONFIG, "agent.agent_timeout": 3}
            agent_worker.Config.get = staticmethod(config.__getitem__)

        class Channel:
            def __init__(self):
                self.request = None
                self.closed = threading.Event()

            def exec_command(self, command):
                assert "cd -- /remote/tasks/PRJ-9 && exec " in command

            def makefile_stdin(self, mode):
                return self

            def makefile(self, mode):
                return self

            def write(self, data):
                self.request = json.loads(data)

            def flush(self):
                pass

            def readline(self):
                request = self.request
                method = request["method"]
                if method == "initialize":
                    assert request["params"]["protocolVersion"] == 1
                    result = {"protocolVersion": 1}
                elif method == "session/new":
                    assert request["params"]["cwd"] == "/remote/tasks/PRJ-9"
                    result = {"sessionId": "session-one"}
                else:
                    assert method == "session/prompt"
                    assert request["params"]["sessionId"] == "session-one"
                    sessions.put((os.getpid(), "PRJ-9"))
                    while not self.closed.is_set():
                        if remote_outcome != "timeout" and release.wait(0.02):
                            break
                        self.closed.wait(0.01)
                    if self.closed.is_set():
                        return b""
                    if remote_outcome == "failure":
                        return (json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                            "error": {"code": -1, "message": "failed"}}) + "\n").encode()
                    result = {"stopReason": "end_turn"}
                return (json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n").encode()

            def recv_stderr(self, size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return 0

            def close(self):
                self.closed.set()

        channel = Channel()

        class SSH:
            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                assert isinstance(policy, agent_remote.paramiko.RejectPolicy)

            def connect(self, **kwargs):
                with connections.get_lock():
                    connections.value += 1
                assert kwargs["hostname"] == "example.invalid"
                assert kwargs["port"] == 2222

            def get_transport(self):
                return self

            def set_keepalive(self, seconds):
                assert seconds > 0

            def open_session(self, **kwargs):
                return channel

            def open_sftp(self):
                return self

            def normalize(self, path):
                return path

            def lstat(self, path):
                import stat
                raise OSError(2, "missing")

            def mkdir(self, path):
                assert path == "/remote/tasks/PRJ-9"

            def putfo(self, stream, path):
                assert isinstance(stream, io.BytesIO)
                assert path == "/remote/tasks/PRJ-9/TASK.md"
                assert stream.read().decode().startswith(
                    CONFIG["agent.prompt"] + "\n\n# PRJ-9 — Полное название\n\n")

            def close(self):
                pass

        agent_remote.paramiko.SSHClient = SSH
    else:
        agent_worker.run_remote = transport
    barrier.wait(timeout=20)
    results.put((os.getpid(), agent_worker.execute()))


def _claim_and_wait(database, files, ready, release):
    board_store.DATABASE_PATH = Path(database)
    board_store.FILES_DIR = Path(files)
    ready.put(agent_store.claim(60))
    release.wait(30)


def _stop_processes(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "ssh-success", "ssh-failure", "ssh-timeout"])
def test_concurrent_executors_claim_only_first_task(database, worker, outcome):
    selected = _seed_tasks()
    success = outcome in ("success", "ssh-success")
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    release = ctx.Event()
    sessions, results = ctx.Queue(), ctx.Queue()
    connections = ctx.Value("i", 0)
    processes = [
        ctx.Process(target=_concurrent_worker,
                    args=(*database, barrier, release, sessions, results, outcome, connections))
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        owner_pid, claimed_task = sessions.get(timeout=20)
        assert claimed_task == selected
        # Every non-owner must exit while the owner's response is still gated.
        exits = [results.get(timeout=20) for _ in range(3)]
        assert all(pid != owner_pid and code == 0 for pid, code in exits)
        rows = _runs()
        assert len(rows) == 1
        run = rows[0]
        assert run["state"] == "RUNNING"
        assert run["pid"] == owner_pid
        assert run["process_token"] == agent_store.process_identity(owner_pid)
        assert run["session_id"] == "session-one"
        assert run["created_at"] <= run["started_at"]
        assert run["finished_at"] is None
        if not outcome.startswith("ssh-"):
            assert "План: выполнить задачу" in run["log"]
        assert run["timeout"] == (3 if outcome == "ssh-timeout" else CONFIG["agent.agent_timeout"])
        tasks = {task["task_id"]: task for task in board_store.list_tasks()}
        assert tasks[selected]["status"] == "IN PROGRESS"
        assert tasks[selected]["is_error"] is False
        assert all(task["status"] == "OPEN" for key, task in tasks.items() if key != selected)
        release.set()
        assert results.get(timeout=20) == (owner_pid, 0 if success else 1)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        # Queue.empty() is unreliable: check with a bounded blocking read.
        from queue import Empty
        with pytest.raises(Empty):
            sessions.get(timeout=0.2)
        assert connections.value == (1 if outcome.startswith("ssh-") else 0)
        task = board_store.get_task(selected)
        assert task["status"] == ("REVIEW" if success else "WAIT")
        assert task["is_error"] is (not success)
        run = _runs()[0]
        assert run["state"] == ("SUCCEEDED" if success else "FAILED")
        assert run["finished_at"] >= run["started_at"]
        assert run["session_id"] == "session-one"
        assert run["stop_reason"] == ("end_turn" if success else None)
        assert bool(run["error"]) is (not success)
        if outcome == "ssh-timeout":
            assert "timed out" in run["error"]
            # Transport starts its monotonic deadline just before on_started;
            # persisted wall-clock timestamps can differ by a few milliseconds.
            assert 2.9 <= run["finished_at"] - run["started_at"] < 6
        assert len(_runs()) == 1
        assert all(task["status"] == "OPEN" for task in board_store.list_tasks() if task["task_id"] != selected)
        assert agent_store.active_run() is None
    finally:
        release.set()
        _stop_processes(processes)
        for queue in (sessions, results):
            queue.close()
            queue.join_thread()


def test_no_open_tasks_never_calls_transport(worker, monkeypatch):
    board_store.create_task("PRJ-1", "Backlog", "Details", [])
    monkeypatch.setattr(worker, "run_remote", lambda *args, **kwargs: pytest.fail("Unexpected SSH"))
    assert worker.execute() == 0
    assert _runs() == []
    assert board_store.get_task("PRJ-1")["status"] == "BACKLOG"


def test_worker_passes_full_config_context_and_persists_callbacks(worker, monkeypatch):
    content = "Полный текст\n'$(not shell)'\n" * 100
    board_store.create_task("PRJ-12", "Task title", content, [("notes.txt", "text/plain", b"attachment")])
    with board_store.connect() as connection:
        connection.execute("UPDATE board_tasks SET status = 'OPEN'")
    keys = []

    def get_config(key):
        keys.append(key)
        return CONFIG[key]

    monkeypatch.setattr(worker.Config, "get", staticmethod(get_config))

    def transport(task, attachments, config, on_session, on_log, on_started, on_message):
        assert task["task_id"] == "PRJ-12"
        assert task["title"] == "Task title"
        assert task["description"] == content.strip()
        assert config == CONFIG
        assert len(attachments) == 1
        path, name = attachments[0]
        assert path.parent == board_store.FILES_DIR
        assert path.read_bytes() == b"attachment"
        assert name == "notes.txt"
        assert _runs()[0]["state"] == "PREPARING"
        on_started()
        assert _runs()[0]["state"] == "RUNNING"
        on_session("session-context")
        assert _runs()[0]["session_id"] == "session-context"
        on_log("first\n")
        on_log("second\n")
        on_message("streamed")
        assert _runs()[0]["log"] == "first\nsecond\n"
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(worker, "run_remote", transport)
    assert worker.execute() == 0
    assert set(keys) == set(CONFIG)
    assert len(keys) == len(CONFIG)
    assert _runs()[0]["state"] == "SUCCEEDED"
    assert _runs()[0]["session_id"] == "session-context"
    assert _runs()[0]["log"] == "first\nsecond\n"


@pytest.mark.parametrize("timeout", [0, -1, "invalid", float("inf"), float("nan")])
def test_invalid_timeout_finishes_without_transport(worker, monkeypatch, timeout):
    selected = _seed_tasks()
    config = {**CONFIG, "agent.agent_timeout": timeout}
    monkeypatch.setattr(worker.Config, "get", staticmethod(config.__getitem__))
    monkeypatch.setattr(worker, "run_remote", lambda *args, **kwargs: pytest.fail("Unexpected SSH"))
    assert worker.execute() == 1
    assert board_store.get_task(selected)["status"] == "WAIT"
    assert board_store.get_task(selected)["is_error"] is True
    assert _runs()[0]["state"] == "FAILED"


def test_unexpected_transport_error_does_not_leak_secrets(worker, monkeypatch, caplog, capsys):
    selected = _seed_tasks()
    secret = CONFIG["remote_server.password"]

    def transport(*args, **kwargs):
        raise RuntimeError(secret + " PRIVATE KEY CONTENT")

    monkeypatch.setattr(worker, "run_remote", transport)
    assert worker.execute() == 1
    captured = capsys.readouterr()
    combined = caplog.text + captured.out + captured.err + repr(_runs())
    assert secret not in combined
    assert "PRIVATE KEY CONTENT" not in combined
    assert board_store.get_task(selected)["is_error"] is True


@pytest.mark.parametrize("expired", [False, True], ids=["killed-owner", "expired-matching-owner"])
def test_recovery_releases_dead_or_expired_process(database, expired):
    selected = _seed_tasks()
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_claim_and_wait, args=(*database, ready, release))
    process.start()
    try:
        run = ready.get(timeout=15)
        assert run["task_id"] == selected
        active = agent_store.active_run()
        assert active["pid"] == process.pid
        assert active["process_token"] == agent_store.process_identity(process.pid)
        if expired:
            with board_store.connect() as connection:
                connection.execute("UPDATE agent_runs SET created_at = ? WHERE id = ?", (time.time() - 120, run["id"]))
        else:
            process.kill()
            process.join(timeout=5)
            assert not process.is_alive()
        agent_store.recover()
        process.join(timeout=5)
        assert not process.is_alive()
        assert agent_store.active_run() is None
        old = _runs()[0]
        assert old["state"] == "FAILED"
        assert old["finished_at"] is not None
        assert old["error"]
        task = board_store.get_task(selected)
        assert task["status"] == "WAIT" and task["is_error"] is True
        replacement = agent_store.claim(60)
        assert replacement["id"] != run["id"]
        assert replacement["task_id"] != selected
        assert board_store.get_task(selected)["status"] == "WAIT"
        agent_store.finish(replacement["id"], "Test cleanup")
    finally:
        # A killed process may hold Event's condition lock; never set that
        # event after killing its waiter (multiprocessing locks are not robust).
        _stop_processes([process])
        ready.close()
        ready.join_thread()


@pytest.mark.parametrize("index", ["agent_single_active", "agent_task_active"])
def test_database_partial_unique_indexes_enforce_exclusivity(database, index):
    _seed_tasks()
    claimed = agent_store.claim(60)
    other_index = "agent_task_active" if index == "agent_single_active" else "agent_single_active"
    with board_store.connect() as connection:
        indexes = {row["name"]: dict(row) for row in connection.execute("PRAGMA index_list(agent_runs)")}
        for name in ("agent_single_active", "agent_task_active"):
            assert indexes[name]["unique"] == 1
            assert indexes[name]["partial"] == 1
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Isolate each constraint to prove both are independently enforced.
            connection.execute(f"DROP INDEX {other_index}")
            task_pk = connection.execute("SELECT task_pk FROM agent_runs WHERE id = ?", (claimed["id"],)).fetchone()[0]
            if index == "agent_single_active":
                task_pk = connection.execute("SELECT id FROM board_tasks WHERE id != ? LIMIT 1", (task_pk,)).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO agent_runs(id, task_pk, state, pid, process_token, created_at, timeout) VALUES ('collision', ?, 'RUNNING', ?, 'token', ?, 60)",
                    (task_pk, os.getpid(), time.time()),
                )
        finally:
            connection.rollback()
    agent_store.finish(claimed["id"], "Test cleanup")
    assert agent_store.claim(60) is not None


def test_launcher_detaches_without_reserving_or_passing_task_id(database, monkeypatch):
    launcher = importlib.import_module("agent_launcher")
    _seed_tasks()
    before = _snapshot()
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=12345, wait=lambda: 0)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    assert launcher.launch() == 12345
    assert _snapshot() == before
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, str(Path(launcher.__file__).with_name("agent_worker.py")),
                    "--database", str(database[0].resolve()), "--files", str(database[1].resolve())]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert all(kwargs[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))


@pytest.mark.parametrize("stale", [False, True], ids=["empty-runs", "stale-run"])
def test_launcher_popen_failure_keeps_database_and_hides_secrets(database, monkeypatch, caplog, capsys, stale):
    launcher = importlib.import_module("agent_launcher")
    _seed_tasks()
    if stale:
        agent_store.claim(60)
        with board_store.connect() as connection:
            connection.execute("UPDATE agent_runs SET process_token = 'dead-process-token'")
    before = _snapshot()
    secret = CONFIG["remote_server.password"]

    def popen(*args, **kwargs):
        raise OSError(secret + " PRIVATE KEY CONTENT")

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError) as error:
        launcher.launch()
    assert _snapshot() == before
    captured = capsys.readouterr()
    output = str(error.value) + caplog.text + captured.out + captured.err
    assert secret not in output
    assert "PRIVATE KEY CONTENT" not in output
    assert caplog.records


def test_active_run_repeated_launch_never_creates_process(database, monkeypatch):
    launcher = importlib.import_module("agent_launcher")
    _seed_tasks()
    agent_store.claim(60)
    before = _snapshot()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Duplicate process"))
    assert launcher.launch() is None
    assert launcher.launch() is None
    assert _snapshot() == before
