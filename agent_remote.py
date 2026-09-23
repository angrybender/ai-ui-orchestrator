"""SSH/SFTP ACP transport; persistence and process ownership belong to the caller."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import posixpath
import re
import shlex
import threading
import time
import unicodedata
import os
from typing import Callable

import paramiko
from task_init import InitError, run_init
from task_context import prepare_directory, task_prompt
from agent_control import AgentControl, CANCEL_TIMEOUT, POLL_INTERVAL

CONNECT_TIMEOUT = 20
STDERR_JOIN_TIMEOUT = 0.2


def _open_raw_log(task_id: str):
    path = Path(__file__).resolve().parent / "data" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    prefix = "test-" if os.environ.get("PYTEST_CURRENT_TEST") else ""
    return (path / f"{prefix}{time.strftime('%Y%m%d-%H%M%S')}-{task_id}-{threading.get_ident()}.log").open("ab")


def _raw_log(handle, direction: bytes, payload: bytes) -> None:
    if handle is not None:
        handle.write(direction + payload)
        handle.flush()


class RemoteAgentError(RuntimeError):
    """Only fixed, safe messages cross the transport boundary."""

    def __init__(self, message: str, stop_reason: str | None = None, *, uncertain=False):
        super().__init__(message)
        self.stop_reason = stop_reason
        self.uncertain = uncertain


class ACPError(RuntimeError):
    def __init__(self, message: str, *, session_missing=False):
        super().__init__(message)
        self.session_missing = session_missing


def _session_missing(parts, session_id):
    """Only explicit missing-session diagnostics permit a fresh session."""
    return any(re.search(
        r"\bsession(?:\s+['\"]?" + re.escape(session_id) + r"['\"]?)?\s+(?:was\s+|is\s+)?"
        r"(?:not\s+found|does\s+not\s+exist|doesn't\s+exist)\b"
        r"|\b(?:unknown|nonexistent|non-existent)\s+session\b",
        part, re.IGNORECASE) for part in parts)


def _name(value: str) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and value not in (".", "..")
            and not any(c in "/\\" or unicodedata.category(c).startswith("C") for c in value))


def _prepare(task, attachments, config):
    task_id = task.get("task_id")
    if not _name(task_id):
        raise ValueError
    if any(not isinstance(task.get(k), str) for k in ("title", "description")):
        raise ValueError
    host = config.get("remote_server.host")
    username = config.get("remote_server.username")
    shell = config.get("agent.shell")
    base = config.get("tasks.base_dir")
    password = config.get("remote_server.password") or ""
    key = config.get("remote_server.ssh_key") or ""
    if not all(isinstance(v, str) for v in (host, username, shell, base, password, key)):
        raise ValueError
    if not host.strip() or not username.strip() or not shell.strip():
        raise ValueError
    if any(unicodedata.category(c).startswith("C") for c in host + username + base) or "\x00" in shell:
        raise ValueError
    if not base.startswith("/") or "\\" in base or not (key or password):
        raise ValueError
    # Bracketed IPv6 may carry a port; bare IPv6 uses the default port.
    port = 22
    if host.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", host)
        if not match:
            raise ValueError
        host, number = match.groups()
        port = int(number) if number else 22
    elif host.count(":") == 1:
        host, number = host.rsplit(":", 1)
        port = int(number)
    if not host or any(c.isspace() for c in host) or not 1 <= port <= 65535:
        raise ValueError
    timeout_value = config.get("agent.agent_timeout")
    if isinstance(timeout_value, bool):
        raise ValueError
    timeout = float(timeout_value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError
    names, used = [], {"task.md"}
    for local, original in attachments:
        if not isinstance(original, str):
            raise ValueError
        name = original.replace("\\", "/").rsplit("/", 1)[-1]
        if not _name(name) or name.casefold() == "task.md" or not Path(local).is_file():
            raise ValueError
        stem, ext = posixpath.splitext(name)
        candidate, n = name, 2
        while candidate.casefold() in used:
            candidate = f"{stem}-{n}{ext}"
            n += 1
        used.add(candidate.casefold())
        names.append((Path(local), candidate))
    base = posixpath.normpath("/" + base.lstrip("/"))
    cwd = posixpath.normpath(posixpath.join(base, task_id))
    if posixpath.dirname(cwd) != base or cwd == base:
        raise ValueError
    listing = "\n".join(f"- {name}" for _, name in names) or "Нет вложений."
    text = f"# {task_id} — {task['title']}\n\n{task['description']}\n\n## Вложения\n\n{listing}\n"
    system_prompt = config.get("agent.prompt")
    if system_prompt:
        text = f"{system_prompt}\n\n{text}"
    secrets = [password, key]
    if key:
        if not Path(key).is_absolute() or not Path(key).is_file():
            raise ValueError
        secrets.append(Path(key).read_text(encoding='utf-8'))
    auth = {"key_filename": key} if key else {"password": password}
    return host, port, username, auth, base, cwd, shell, timeout, names, text, tuple(secrets)


class _Diagnostics:
    """Do not deliver chunks: a secret may span any number of ACP/stderr frames."""

    def __init__(self, secrets):
        self.lock = threading.Lock()
        self.parts = {"acp": [], "stderr": [], "exception": []}
        self.message_parts = []
        self.sealed = False
        self.secrets = {s for s in secrets if s}
        for secret in tuple(self.secrets):
            if "PRIVATE KEY" in secret:
                lines = [s.strip() for s in secret.splitlines() if s.strip() and "---" not in s]
                self.secrets.update(lines)
                self.secrets.add("".join(lines))

    def safe(self, text):
        # Include unterminated PEM blocks and a truncated opening marker.
        text = re.sub(r"-----BEGIN [^\r\n]*PRIVATE KEY-----.*?(?:-----END [^\r\n]*PRIVATE KEY-----|\Z)",
                      "[REDACTED]", text, flags=re.S)
        text = re.sub(r"-----BEGIN [^\r\n]*(?:\Z)", "[REDACTED]", text)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
            # Preserve diagnostics on failure without leaking a final partial secret.
            for size in range(min(len(secret) - 1, len(text)), 0, -1):
                if text.endswith(secret[:size]):
                    text = text[:-size] + "[REDACTED]"
                    break
        for size in range(min(len("-----BEGIN "), len(text)), 0, -1):
            if text.endswith("-----BEGIN "[:size]):
                text = text[:-size] + "[REDACTED]"
                break
        return text

    def append(self, stream, text):
        with self.lock:
            if text and not self.sealed:
                self.parts[stream].append(text)

    def finish(self):
        with self.lock:
            self.sealed = True
            # Sanitize streams separately; no inserted separators between chunks.
            return "\n".join(self.safe("".join(parts)) for parts in self.parts.values() if parts)


def _event(message, diagnostics, on_message=None, raw_log=None):
    if raw_log is not None:
        _raw_log(raw_log, b"IN ", json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    params = message.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("update"), dict):
        raise ValueError
    update = params["update"]
    kind = update.get("sessionUpdate")
    if kind in ("agent_thought_chunk", "user_message_chunk", "tool_call", "tool_call_update"):
        with diagnostics.lock:
            diagnostics.message_parts.clear()
    if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
        content = update.get("content", {})
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            diagnostics.append("acp", content["text"])
            if kind == "agent_message_chunk" and on_message is not None:
                with diagnostics.lock:
                    if diagnostics.sealed:
                        return
                    diagnostics.message_parts.append(content["text"])
                    accumulated = "".join(diagnostics.message_parts)
                on_message(diagnostics.safe(accumulated))
    elif kind == "plan":
        for entry in update.get("entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                diagnostics.append("acp", entry["content"])
    elif kind in ("tool_call", "tool_call_update"):
        title = update.get("title")
        if isinstance(title, str):
            name, sep, detail = title.partition(":")
            if sep and name.strip() and detail.strip():
                diagnostics.append("acp", title)


def _reject_constant(_value):
    raise ValueError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _request(stdin, stdout, request_id, method, params, diagnostics, cancelled, on_message=None, raw_log=None, sender=None):
    def send(message):
        if sender is not None:
            return sender(message)
        if cancelled.is_set():
            raise ValueError
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        _raw_log(raw_log, b"OUT ", payload)
        stdin.write(payload)
        stdin.flush()

    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while not cancelled.is_set():
        line = stdout.readline()
        _raw_log(raw_log, b"IN ", line)
        if not line or not line.endswith(b"\n"):
            raise ValueError
        message = json.loads(line.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError
        if "error" in message:
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise ValueError
            error = message.get("error")
            if isinstance(error, dict):
                message_text = error.get("message")
                data = error.get("data")
                details = data.get("details") if isinstance(data, dict) else None
                parts = [value.strip() for value in (message_text, details)
                         if isinstance(value, str) and value.strip()]
                raise ACPError(
                    f"{method}: {': '.join(parts)}" if parts else f"{method}: ACP request failed",
                    session_missing=method == "session/load" and _session_missing(parts, params.get('sessionId', '')))
            raise ValueError
        if "method" in message:
            if not isinstance(message["method"], str) or "result" in message:
                raise ValueError
            if "id" in message:
                if type(message["id"]) not in (int, str):
                    raise ValueError
                send({"jsonrpc": "2.0", "id": message["id"],
                      "error": {"code": -32601, "message": "Method not supported"}})
            elif message["method"] == "session/update":
                _event(message, diagnostics, on_message, raw_log)
            continue
        if type(message.get("id")) is not int or message["id"] != request_id:
            raise ValueError
        if not isinstance(message.get("result"), dict):
            raise ValueError
        return message["result"]
    raise ValueError


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass


def run_remote(task: dict, attachments: list[tuple[Path, str]], config: dict,
               on_session: Callable[[str], None], on_log: Callable[[str], None],
               on_started: Callable[[], None], on_message: Callable[[str], None] | None = None) -> dict:
    """Return sessionId/stopReason or raise RemoteAgentError with a safe message.

    Callbacks persist state in the caller; on_session/on_started run in the ACP
    worker and must be short, thread-safe operations. Logs are delivered once on
    completion (also on failure), never as unredacted streaming chunks.
    """
    cycle = task.get('_cycle')
    raw_log = None
    try:
        raw_log = _open_raw_log(task.get("task_id", "unknown"))
        prepared = _prepare(task, attachments, config)
    except Exception as e:
        if raw_log is not None:
            raw_log.close()
        raise RemoteAgentError(f"Invalid remote agent configuration or task context: {e}") from None
    host, port, username, auth, base, cwd, shell, timeout, names, text, secrets = prepared
    diagnostics = _Diagnostics(secrets)
    ssh = sftp = channel = None
    stderr_thread = None
    worker = None
    cancelled = threading.Event()
    finished = threading.Event()
    outcome = {}
    error = None
    streams = []
    control = None
    cancel_thread = None
    user_cancelled = False
    uncertain = False
    try:
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(hostname=host, port=port, username=username, **auth,
                    allow_agent=False, look_for_keys=False, timeout=CONNECT_TIMEOUT,
                    auth_timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT)
        transport = ssh.get_transport()
        if transport is None:
            raise ValueError
        transport.set_keepalive(30)
        sftp = ssh.open_sftp()
        # Resolve the parent before creating anything; mkdir is exclusive, never stat+overwrite.
        base = posixpath.normpath(sftp.normalize(base))
        if not base.startswith("/"):
            raise ValueError
        cwd = posixpath.join(base, task["task_id"])
        if posixpath.dirname(posixpath.normpath(cwd)) != base:
            raise ValueError
        resume = cycle.reuse(cwd) if cycle else bool(task.get("session_id"))
        context_dir = prepare_directory(sftp, cwd, task['task_id'], resume)
        if not resume:
            sftp.putfo(io.BytesIO(text.encode("utf-8")), posixpath.join(context_dir, "TASK.md"))
            for local, name in names:
                sftp.put(str(local), posixpath.join(context_dir, name))
        if cycle:
            cycle.prepared(cwd)
            cycle.check()
        enabled = bool((config.get('tasks.init_script_path') or '').strip() or (config.get('tasks.init_script_text') or '').strip())
        initialized = cycle.run.get('init_succeeded') if cycle else task.get('init_succeeded')
        if enabled and not initialized and not task.get('session_id'):
            if cycle:
                cycle.phase('Init')
            run_init(transport, sftp, cwd, config, sanitize=diagnostics.safe,
                     active=cycle.active if cycle else lambda: True,
                     on_pid=cycle.pid if cycle else lambda pid: None, on_log=on_log)
            if cycle:
                cycle.initialized()
        if cycle:
            cycle.phase('Agent')
        prompt = task.get("comment") or task_prompt(task['task_id'])
        if task.get('comment') and not task.get('session_id'):
            prompt = task_prompt(task['task_id']) + '\n\n' + task['comment']
        if not isinstance(prompt, str) or "\x00" in prompt:
            raise ValueError
        # ACP carries the prompt via session/prompt. Keep optional substitution
        # for existing shell templates, but plain ACP commands need no template.
        shell = shell.replace('${PROMPT}', shlex.quote(prompt))
        if task.get('session_id') and config.get('agent.continue_session_arg'):
            shell += ' ' + config['agent.continue_session_arg']
        control = AgentControl(transport, cancelled, lambda payload: _raw_log(raw_log, b'OUT ', payload), cycle)
        command = control.command(cwd, shell)
        # Own the channel before exec_command: it may itself block waiting for SSH acknowledgement.
        channel = transport.open_session(timeout=CONNECT_TIMEOUT)

        def stderr_reader():
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while True:
                    chunk = channel.recv_stderr(4096)
                    if not chunk:
                        break
                    diagnostics.append("stderr", decoder.decode(chunk))
                    _raw_log(raw_log, b"ERR ", chunk)
            except Exception:
                pass
            finally:
                diagnostics.append("stderr", decoder.decode(b"", final=True))

        def execute():
            nonlocal stderr_thread
            stage = "remote command startup"
            try:
                stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
                stderr_thread.start()
                if cycle:
                    cycle.check()
                on_started()
                channel.exec_command(command)
                if cancelled.is_set():
                    return
                stdin = channel.makefile_stdin("wb")
                stdout = channel.makefile("rb")
                streams.extend((stdin, stdout))
                stage = "remote process handshake"
                if not control.handshake(stdin, stdout):
                    return
                stage = "initialize"
                initialized = _request(stdin, stdout, 1, "initialize", {
                    "protocolVersion": 1, "clientCapabilities": {},
                    "clientInfo": {"name": "task-orchestrator", "version": "1.0.0"}}, diagnostics, cancelled, on_message, raw_log, control.send)
                if type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
                    raise ValueError
                stage = "session/load" if task.get("session_id") else "session/new"
                request_id = 2
                session_id = None
                session_prompt = prompt
                if task.get("session_id"):
                    session_id = task.get("session_id")
                    if not isinstance(session_id, str) or not session_id.strip():
                        raise ACPError("session/load: missing saved session id")
                    try:
                        _request(stdin, stdout, request_id, "session/load", {
                            "sessionId": session_id, "cwd": cwd, "mcpServers": []}, diagnostics, cancelled, on_message, raw_log, control.send)
                    except ACPError as exception:
                        if not exception.session_missing:
                            raise
                        diagnostics.append("exception", "Saved ACP session not found; creating a new session.\n")
                        session_id = None
                        request_id += 1
                        session_prompt = task_prompt(task['task_id'])
                        if task.get('comment'):
                            session_prompt += '\n\n' + task['comment']
                if session_id is None:
                    stage = "session/new"
                    session = _request(stdin, stdout, request_id, "session/new", {"cwd": cwd, "mcpServers": []}, diagnostics, cancelled, on_message, raw_log, control.send)
                    session_id = session.get("sessionId")
                    if not isinstance(session_id, str) or not session_id.strip() or diagnostics.safe(session_id) != session_id:
                        raise ValueError("Missing, invalid or unsafe sessionId")
                if cancelled.is_set():
                    return
                stage = "persist sessionId"
                on_session(session_id)
                stage = "session/prompt"
                result = _request(stdin, stdout, request_id + 1, "session/prompt", {
                    "sessionId": session_id, "prompt": [{"type": "text", "text": session_prompt}]}, diagnostics, cancelled, on_message, raw_log, control.send)
                reason = result.get("stopReason")
                if reason == 'cancelled' and control.requested.is_set():
                    outcome.update(sessionId=session_id, stopReason=reason)
                    return
                if reason not in ("end_turn", "max_tokens", "max_turn_requests", "refusal"):
                    if reason in ("cancelled", "canceled", "error", "failed"):
                        outcome["stop_reason"] = reason
                    raise ValueError
                if channel.exit_status_ready() and channel.recv_exit_status() != 0:
                    raise ValueError
                outcome.update(sessionId=session_id, stopReason=reason)
            except ACPError as exception:
                outcome["error"] = diagnostics.safe(str(exception))
                diagnostics.append("exception", outcome["error"])
                outcome["failed"] = True
            except Exception as exception:
                if not cancelled.is_set():
                    outcome["error"] = diagnostics.safe(f"{stage}: {type(exception).__name__}: {exception}")
                    diagnostics.append("exception", outcome["error"])
                    outcome["failed"] = True
            finally:
                finished.set()

        worker = threading.Thread(target=execute, daemon=True)
        started = time.monotonic()
        worker.start()
        cancel_deadline = None
        while True:
            if cycle and not user_cancelled and not cycle.active():
                user_cancelled = True
                control.requested.set()
                cancel_deadline = time.monotonic() + CANCEL_TIMEOUT
                cycle.cancelling()

                def cancel_session():
                    try:
                        control.request_cancel()
                    except Exception:
                        diagnostics.append('exception', 'Unable to send ACP session/cancel.')

                cancel_thread = threading.Thread(target=cancel_session, daemon=True)
                cancel_thread.start()
            if finished.is_set():
                break
            if user_cancelled:
                if (control.cancel_sent.is_set() and not control.prompt_sent) or time.monotonic() >= cancel_deadline:
                    break
            elif time.monotonic() - started >= timeout:
                error = "Remote agent timed out."
                break
            deadline = cancel_deadline if user_cancelled else started + timeout
            finished.wait(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
        if user_cancelled:
            error = 'Remote agent cancelled.'
            outcome['stop_reason'] = 'cancelled'
        elif not error and (outcome.get("failed") or not outcome):
            error = diagnostics.safe(outcome.get("error") or "Remote agent execution failed.")
    except InitError:
        raise
    except Exception as exception:
        detail = diagnostics.safe(str(exception)).strip()
        error = detail or f"Remote agent connection failed ({type(exception).__name__})."
        diagnostics.append("stderr", f"{type(exception).__name__}: {error}")
    finally:
        cancelled.set()
        if control is not None:
            try:
                uncertain = not control.stop()
            except Exception:
                uncertain = True
            if uncertain:
                error = (error or 'Remote agent cleanup failed.') + '\nRemote stop unconfirmed; retry blocked pending verification.'
        _close(channel)
        if stderr_thread is not None:
            stderr_thread.join(STDERR_JOIN_TIMEOUT)
        _close(ssh)
        _close(sftp)
        if worker is not None and worker.is_alive():
            worker.join(STDERR_JOIN_TIMEOUT)
        if cancel_thread is not None:
            cancel_thread.join(STDERR_JOIN_TIMEOUT)
        for stream in streams:
            _close(stream)
        if raw_log is not None:
            raw_log.close()
        safe_log = diagnostics.finish()
        if error == "Remote agent execution failed." and safe_log:
            error = f"Remote agent execution failed: {safe_log}"
        if safe_log:
            try:
                on_log(safe_log)
            except Exception:
                error = "Remote agent log persistence failed."
    if error:
        reason = 'cancelled' if user_cancelled else outcome.get('stop_reason')
        raise RemoteAgentError(error, stop_reason=reason, uncertain=uncertain) from None
    return {"sessionId": outcome["sessionId"], "stopReason": outcome["stopReason"]}
