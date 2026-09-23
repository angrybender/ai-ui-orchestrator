"""Local subprocess-backed SSH fixture for cancellation tests (no remote access)."""
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

from test_task_init import Channel

AGENT = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(180)'])
Path('child.pid').write_text(str(child.pid))
mode = sys.argv[1]
prompt_id = None
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'initialize':
        if mode == 'before-session':
            Path('initialize.ready').touch()
            time.sleep(180)
        result = {'protocolVersion': 1}
    elif method == 'session/new':
        result = {'sessionId': 'local-session'}
    elif method == 'session/load':
        result = {}
    elif method == 'session/prompt':
        prompt_id = request['id']
        Path('prompt.ready').touch()
        continue
    elif method == 'session/cancel':
        assert 'id' not in request
        assert request['params']['sessionId'] == 'local-session'
        Path('cancel.received').write_text(str(time.monotonic()))
        if mode == 'ignore':
            continue
        if mode == 'delayed':
            time.sleep(0.3)
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': {
            'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'late reply'}}}}), flush=True)
        print(json.dumps({'jsonrpc': '2.0', 'id': prompt_id,
                          'result': {'stopReason': 'end_turn' if mode == 'race' else 'cancelled'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class LocalChannel(Channel):
    def exec_command(self, command):
        self.process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, process_group=0)

    def makefile_stdin(self, mode):
        return self.process.stdin

    def makefile(self, mode):
        return self.process.stdout


class LocalSSH:
    def __init__(self):
        self.channels = []
        self.closed = False

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        pass

    def get_transport(self):
        return self

    def set_keepalive(self, seconds):
        pass

    def is_active(self):
        return not self.closed

    def open_session(self, timeout):
        channel = LocalChannel()
        self.channels.append(channel)
        return channel

    def open_sftp(self):
        class SFTP:
            def normalize(self, path):
                return str(Path(path).resolve())
            def mkdir(self, path):
                Path(path).mkdir()
            def lstat(self, path):
                return Path(path).lstat()
            def putfo(self, stream, path):
                Path(path).write_bytes(stream.read())
            def close(self):
                pass
        return SFTP()

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, directory, monkeypatch, mode='cooperative'):
        import agent_remote
        import agent_store
        import agent_worker
        import board_store
        self.directory = directory
        self.cwd = directory / 'CANCEL-1'
        script = directory / 'fake_agent.py'
        script.write_text(AGENT)
        self.config = {'remote_server.host': 'fixture.invalid', 'remote_server.username': 'fixture',
                       'remote_server.password': 'fixture-secret', 'remote_server.ssh_key': '',
                       'tasks.base_dir': str(directory), 'agent.shell': shlex.join([sys.executable, '-u', str(script), mode]),
                       'agent.agent_timeout': 120}
        self.ssh = LocalSSH()
        monkeypatch.setattr(agent_remote.paramiko, 'SSHClient', lambda: self.ssh)
        monkeypatch.setattr(agent_remote, '_open_raw_log', lambda _: io.BytesIO())
        from types import SimpleNamespace
        monkeypatch.setattr(agent_worker, 'Config', SimpleNamespace(get=self.config.get))
        board_store.init_database()
        agent_store.init_database()
        board_store.create_task('CANCEL-1', 'Cancel a running agent', 'Exercise explicit ACP cancellation', [])
        board_store.move_task('CANCEL-1', 'OPEN', 0)
        self.worker = threading.Thread(target=agent_worker.execute)

    def start(self):
        self.worker.start()
        deadline = time.monotonic() + 5
        while not (self.cwd / 'prompt.ready').exists():
            if not self.worker.is_alive() or time.monotonic() >= deadline:
                raise AssertionError('Agent did not reach session/prompt')
            time.sleep(0.01)

    def cleanup(self):
        # Only groups created by this fixture; also handles failed assertions.
        import board_store
        with board_store.connect() as db:
            rows = db.execute('SELECT agent_pid FROM agent_runs WHERE agent_pid IS NOT NULL').fetchall()
        for row in rows:
            try:
                os.killpg(row[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.worker.ident is not None:
            self.worker.join(5)
