"""Opt-in SSH/ACP live-test support. Provider keys never enter command lines."""
from __future__ import annotations

from contextlib import suppress
import json
from pathlib import Path
import shlex
import threading
import time

import paramiko

from settings.config import Config
from settings.validation.remote_server import _split_host

CAPABILITIES = {'_meta': {'orchestratorBackgroundTasks': 1, 'jetbrains': {'air': {
    'version': 1, 'capabilities': ['asyncTasks', 'nativeSubagentSessions', 'sessionFailure']}}}}
BACKGROUND = '_orchestrator/background'

LAUNCHER = '''from pathlib import Path
import json, os, sys
root, agent, cwd, invalid = sys.argv[1:]
key = Path(root, 'provider-key').read_text() if invalid != 'invalid' else 'sk-live-test-invalid-credential'
settings = json.loads(Path(root, 'settings.json').read_text())
env = dict(os.environ)
for name in list(env):
    if name.startswith(('OPENAI_', 'ANTHROPIC_')) or name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        env.pop(name)
env.update(LIVE_ACP_ROOT=root, LIVE_ACP_CASE=cwd, PATH=str(Path.home()/'.local/bin')+':'+env.get('PATH', ''))
if agent == 'codex':
    env.update(CODEX_HOME=root+'/codex'+('-invalid' if invalid == 'invalid' else ''), OPENAI_API_KEY=key)
else:
    env.update(CLAUDE_CONFIG_DIR=root+'/claude'+('-invalid' if invalid == 'invalid' else ''),
        ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_BASE_URL=settings['endpoint'].removesuffix('/v1'), ANTHROPIC_MODEL=settings['claude_model'],
        ANTHROPIC_DEFAULT_SONNET_MODEL=settings['claude_model'], ANTHROPIC_DEFAULT_HAIKU_MODEL=settings['claude_model'],
        ANTHROPIC_DEFAULT_OPUS_MODEL=settings['claude_model'], CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    if invalid == 'invalid': env['CLAUDE_CODE_MAX_RETRIES'] = '0'
os.chdir(cwd)
os.execve(settings[agent+'_executable'], [settings[agent+'_executable']], env)
'''

PROCESSES = '''import json, os, pathlib, signal, sys, time
root, case, action = sys.argv[1:]
def scan():
    result=[]
    for path in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            state=(path/'stat').read_text().rsplit(')',1)[1].split()[0]
            env=(path/'environ').read_bytes().split(b'\\0')
            cwd = str((path/'cwd').resolve())
            tagged = ('LIVE_ACP_ROOT='+root).encode() in env and (case == '*' or ('LIVE_ACP_CASE='+case).encode() in env)
            by_cwd = cwd.startswith(root+'/') if case == '*' else cwd == case or cwd.startswith(case+'/')
            if state != 'Z' and (tagged or by_cwd):
                result.append({'pid':int(path.name),'pgid':os.getpgid(int(path.name)), 'command':(path/'comm').read_text().strip(), 'args':(path/'cmdline').read_bytes().replace(b'\\0',b' ').decode(errors='replace')[:500]})
        except (OSError, ValueError):
            continue
    return result
if action == 'stop':
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for row in scan():
            try: os.kill(row['pid'], sig)
            except ProcessLookupError: pass
        deadline=time.monotonic()+2
        while scan() and time.monotonic()<deadline: time.sleep(.05)
print(json.dumps(scan()))
'''


class RpcError(RuntimeError):
    def __init__(self, response):
        self.response = response
        self.code = response.get('code')
        details = response.get('data', {}).get('details') if isinstance(response.get('data'), dict) else None
        message = str(response.get('message', response))
        super().__init__(message+(': '+str(details) if details else ''))


class Environment:
    def __init__(self, key, endpoint, *, codex_model='openai/gpt-6-luna', claude_model='anthropic/claude-sonnet-4.6', codex_binary=None):
        self.key = key
        self.endpoint = endpoint.rstrip('/')
        self.config = Config.snapshot(('remote_server.host', 'remote_server.username', 'remote_server.password', 'remote_server.ssh_key'))
        self.ssh = self.connect()
        self.sftp = self.ssh.open_sftp()
        home = self.command("python3 -c 'from pathlib import Path; print(Path.home())'").strip()
        self.root = self.command('mktemp -d /tmp/acp-routerai-live-XXXXXXXX').strip()
        if not self.root.startswith('/tmp/acp-routerai-live-'):
            raise RuntimeError('Unexpected test directory')
        self.settings = dict(endpoint=self.endpoint, codex_model=codex_model, claude_model=claude_model,
                             codex_executable=codex_binary or home+'/.local/bin/acp-codex-wrapper',
                             claude_executable=home+'/.local/bin/claude-agent-acp-wait')
        self.clients = []
        self.write(self.root+'/provider-key', key, 0o600)
        self.write(self.root+'/settings.json', json.dumps(self.settings), 0o600)
        self.write(self.root+'/launch.py', LAUNCHER, 0o600)
        self.write(self.root+'/processes.py', PROCESSES, 0o600)
        for suffix in ('', '-invalid'):
            for agent in ('codex', 'claude'):
                self.sftp.mkdir(self.root+'/'+agent+suffix, 0o700)
            config = ('model = '+json.dumps(codex_model)+'\nmodel_provider = "routerai"\n'
                      'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nmodel_reasoning_effort = "low"\n'
                      '[features]\nmulti_agent = true\n'
                      '[shell_environment_policy]\ninherit = "all"\n'
                      '[model_providers.routerai]\nname = "RouterAI live test"\n'
                      'base_url = '+json.dumps(self.endpoint)+'\nenv_key = "OPENAI_API_KEY"\n'
                      'wire_api = "responses"\nrequires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
            self.write(self.root+'/codex'+suffix+'/config.toml', config, 0o600)
            self.write(self.root+'/claude'+suffix+'/settings.json', json.dumps({'permissions': {'defaultMode': 'bypassPermissions'}}), 0o600)

    def connect(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        host, port = _split_host(self.config['remote_server.host'])
        auth = ({'key_filename': self.config['remote_server.ssh_key']} if self.config['remote_server.ssh_key']
                else {'password': self.config['remote_server.password']})
        client.connect(hostname=host, port=port, username=self.config['remote_server.username'], **auth,
                       allow_agent=False, look_for_keys=False, timeout=20, auth_timeout=20, banner_timeout=20)
        return client

    def safe(self, value):
        text = str(value)
        for secret in (self.key, self.config.get('remote_server.password'), self.config.get('remote_server.ssh_key')):
            if secret:
                text = text.replace(secret, '[redacted]')
        return text

    def command(self, command, timeout=25):
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)
        try:
            stdin.close()
            result = stdout.read().decode('utf-8', 'replace')
            diagnostic = stderr.read().decode('utf-8', 'replace')
            if stdout.channel.recv_exit_status():
                raise RuntimeError(self.safe(diagnostic or result))
            return result
        finally:
            for stream in (stdin, stdout, stderr):
                with suppress(Exception):
                    stream.close()

    def write(self, path, text, mode=0o600):
        with self.sftp.file(path, 'w') as handle:
            handle.write(text)
        self.sftp.chmod(path, mode)

    def read(self, path):
        try:
            with self.sftp.file(path, 'r') as handle:
                return handle.read().decode('utf-8', 'replace')
        except FileNotFoundError:
            return None

    def processes(self, case='*', stop=False):
        command = ['python3', self.root+'/processes.py', self.root, case, 'stop' if stop else 'list']
        return json.loads(self.command(shlex.join(command)))

    def client(self, agent, case, *, capabilities=None, invalid=False):
        cwd = self.root+'/'+agent+'-'+case
        with suppress(OSError):
            self.sftp.mkdir(cwd, 0o700)
        client = ACPClient(self, agent, cwd, invalid=invalid)
        self.clients.append(client)
        result = client.request('initialize', {'protocolVersion': 1, 'clientCapabilities': capabilities if capabilities is not None else CAPABILITIES,
            'clientInfo': {'name': 'orchestrator-live-tests', 'version': '1.0'}})
        client.initialized = result
        return client

    def cleanup(self):
        for client in self.clients:
            with suppress(Exception):
                client.close()
        self.clients.clear()
        try:
            self.processes(stop=True)
            # This path was created with mktemp for this run, never production.
            self.command('python3 -c '+shlex.quote('import shutil; shutil.rmtree('+repr(self.root)+')'))
        finally:
            self.sftp.close()
            self.ssh.close()


class ACPClient:
    def __init__(self, environment, agent, cwd, *, invalid=False):
        self.env, self.agent, self.cwd = environment, agent, cwd
        self.channel = environment.ssh.get_transport().open_session(timeout=20)
        self.channel.exec_command(shlex.join(['exec', 'setsid', '--wait', 'python3', environment.root+'/launch.py', environment.root, agent, cwd, 'invalid' if invalid else 'valid']))
        self.stdin = self.channel.makefile_stdin('wb')
        self.stdout = self.channel.makefile('rb')
        self.responses = {}
        self.events = []
        self.errors = []
        self.write_lock = threading.Lock()
        self.condition = threading.Condition()
        self.counter = 0
        self.session = None
        self.forced_stop = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._stderr, daemon=True)
        self.stderr_reader.start()

    def send(self, message):
        with self.write_lock:
            self.stdin.write((json.dumps(message)+'\n').encode())
            self.stdin.flush()

    def _stderr(self):
        while not self.channel.closed:
            data = self.channel.recv_stderr(8192)
            if not data:
                break
            self.errors.append(self.env.safe(data.decode('utf-8', 'replace')))

    def _read(self):
        try:
            for line in self.stdout:
                message = json.loads(line)
                if 'method' in message and 'id' in message:
                    params = message.get('params', {})
                    options = params.get('options', [])
                    allowed = next((option for option in options if option.get('kind') in ('allow_once', 'allow_always')), None)
                    if message['method'] == 'session/request_permission' and allowed:
                        self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': {'outcome': 'selected', 'optionId': allowed['optionId']}}})
                    else:
                        self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': 'Not supported by live client'}})
                elif 'id' in message:
                    with self.condition:
                        self.responses[message['id']] = message
                        self.condition.notify_all()
                else:
                    self.events.append(message)
        except Exception as error:
            self.errors.append(self.env.safe(error))
        finally:
            with self.condition:
                self.condition.notify_all()

    def start(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        return request_id

    def wait(self, request_id, timeout=90):
        deadline = time.monotonic()+timeout
        with self.condition:
            while request_id not in self.responses:
                if not self.reader.is_alive():
                    raise RuntimeError('ACP closed: '+''.join(self.errors)[-1500:])
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('ACP request timed out: '+''.join(self.errors)[-1000:])
                self.condition.wait(min(remaining, .2))
            message = self.responses.pop(request_id)
        if 'error' in message:
            raise RpcError(message['error'])
        return message.get('result')

    def request(self, method, params, timeout=90):
        return self.wait(self.start(method, params), timeout)

    def new_session(self):
        result = self.request('session/new', {'cwd': self.cwd, 'mcpServers': []})
        self.session = result['sessionId']
        modes = result.get('modes', {}).get('availableModes', [])
        wanted = ('agent-full-access', 'danger-full-access', 'bypassPermissions')
        mode = next((row['id'] for row in modes if row.get('id') in wanted), None)
        if mode:
            self.request('session/set_mode', {'sessionId': self.session, 'modeId': mode})
        return result

    def prompt(self, text, timeout=150):
        return self.request('session/prompt', {'sessionId': self.session, 'prompt': [{'type': 'text', 'text': text}]}, timeout)

    def begin_prompt(self, text):
        return self.start('session/prompt', {'sessionId': self.session, 'prompt': [{'type': 'text', 'text': text}]})

    def snapshot(self, **extra):
        return self.request(BACKGROUND, {'sessionId': self.session, **extra}, timeout=15)

    def close(self):
        if self.channel.closed:
            for stream in (self.stdin, self.stdout):
                with suppress(Exception):
                    if not stream.closed:
                        stream.close()
            return
        with suppress(Exception):
            self.stdin.close()
        with suppress(Exception):
            self.channel.shutdown_write()
        deadline = time.monotonic()+5
        while not self.channel.exit_status_ready() and time.monotonic() < deadline:
            time.sleep(.05)
        if not self.channel.exit_status_ready():
            self.forced_stop = True
            self.env.processes(self.cwd, stop=True)
        self.channel.close()
        self.reader.join(2)
        self.stderr_reader.join(2)
        with suppress(Exception):
            self.stdout.close()


def poll(predicate, timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.15)
    raise TimeoutError('Live assertion timed out')
