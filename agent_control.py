"""Serialized ACP writes and an owned, gated remote process group."""
import json
import re
import shlex
import threading

from task_init import stop_group

CANCEL_TIMEOUT = 60
STOP_GRACE = 20
POLL_INTERVAL = 0.1


class AgentControl:
    def __init__(self, transport, cancelled, raw_write, cycle=None):
        self.transport = transport
        self.cancelled = cancelled
        self.raw_write = raw_write
        self.cycle = cycle
        self.lock = threading.Lock()
        self.startup_lock = threading.Lock()
        self.requested = threading.Event()
        self.cancel_sent = threading.Event()
        self.stdin = None
        self.pid = None
        self.session_id = None
        self.prompt_sent = False

    @staticmethod
    def command(cwd, shell):
        supervisor = ('printf "ACP_PID:%s\\n" "$$"; IFS= read -r gate; '
                      '[ "$gate" = GO ] || exit 125; exec ' + shell)
        return f'cd -- {shlex.quote(cwd)} && exec setsid --wait bash -c {shlex.quote(supervisor)}'

    def handshake(self, stdin, stdout):
        line = stdout.readline(128)
        if not re.fullmatch(rb'ACP_PID:[1-9][0-9]*\n', line):
            raise ValueError('Invalid remote agent startup handshake')
        # Persist before GO: a dead executor can never leave an untracked agent.
        with self.lock:
            with self.startup_lock:
                if self.cancelled.is_set() or self.requested.is_set():
                    return False
                self.pid = int(line.split(b':')[1])
            if self.cycle:
                self.cycle.agent_pid(self.pid)
            if self.cancelled.is_set() or self.requested.is_set():
                return False
            self.stdin = stdin
            stdin.write(b'GO\n')
            stdin.flush()
        return True

    def send(self, message):
        with self.lock:
            if self.cancelled.is_set() or (self.requested.is_set() and 'method' in message):
                raise ValueError('Remote agent cancelled')
            self._write(message)
            if message.get('method') == 'session/prompt':
                self.session_id = message['params']['sessionId']
                self.prompt_sent = True

    def _write(self, message):
        payload = json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n'
        self.raw_write(payload)
        self.stdin.write(payload)
        self.stdin.flush()

    def request_cancel(self):
        """Run in a sender thread: a blocked SSH write must not block the deadline."""
        self.requested.set()
        try:
            with self.lock:
                if self.prompt_sent and not self.cancelled.is_set():
                    self._write({'jsonrpc': '2.0', 'method': 'session/cancel',
                                 'params': {'sessionId': self.session_id}})
        finally:
            self.cancel_sent.set()

    def stop(self):
        # Freeze startup before reading pid. A blocked GO writer already set pid;
        # Never wait for the writer lock: that could defeat the deadline.
        with self.startup_lock:
            self.cancelled.set()
            pid = self.pid
        confirmed = stop_group(self.transport, pid) if pid else True
        if self.cycle:
            self.cycle.agent_stopped(confirmed)
        return confirmed
