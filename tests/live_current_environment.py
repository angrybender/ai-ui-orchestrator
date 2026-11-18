"""Opt-in live tests using installed agents and their existing remote settings."""
from contextlib import suppress
import json
import shlex

from agent_worker import CONFIG_KEYS
from live_acp_client import Environment, PROCESSES
from settings.config import Config


LAUNCHER = '''import json, os, pathlib, sys
root, agent, cwd = sys.argv[1:]
commands = json.loads(pathlib.Path(root, 'commands.json').read_text())
os.environ['PATH'] = str(pathlib.Path.home()/'.local/bin')+':'+os.environ.get('PATH', '')
os.environ['LIVE_ACP_ROOT'] = root
os.environ['LIVE_ACP_CASE'] = cwd
claude_config = pathlib.Path(root, 'claude-provider')
if agent == 'claude' and (claude_config/'settings.json').exists():
    # Stable across all phases so session/load can read the previous session.
    settings = json.loads((claude_config/'settings.json').read_text())
    os.environ.update(settings['env'])
    os.environ['CLAUDE_CONFIG_DIR'] = str(claude_config)
os.chdir(cwd)
os.execl('/bin/bash', 'bash', '-c', 'exec '+commands[agent])
'''

CACHE_ROOTS = ['.claude/projects', '.claude/sessions', '.qwen/projects', '.qwen/tmp', '.qwen/sessions',
               '.qwen/debug', '.qwen/todos', '.qwen/file-history', '.codex/sessions', '.codex/archived_sessions',
               '.codex/tmp', '.codex/shell_snapshots', '.codex/acp-wrapper-scopes', '.codex/thread-writer-locks']


class CurrentEnvironment(Environment):
    def __init__(self):
        # Do not call Environment.__init__: that creates substitute provider configs.
        self.config = Config.snapshot(CONFIG_KEYS)
        self.key = ''
        self.clients = []
        self.ssh = self.connect()
        self.sftp = self.ssh.open_sftp()
        self.home = self.command("python3 -c 'from pathlib import Path; print(Path.home())'").strip()
        self.root = self.command('mktemp -d /tmp/acp-context-live-XXXXXXXX').strip()
        if not self.root.startswith('/tmp/acp-context-live-'):
            raise RuntimeError('Unexpected live-test root')
        wrapper = self.home+'/.local/bin/acp-wrapper'
        self.commands = {
            'claude': shlex.join([wrapper, self.home+'/.local/bin/claude', '--dangerously-skip-permissions']),
            'codex': shlex.join([wrapper, '/usr/bin/codex', '--yolo']),
            'qwen': self.config['agent.shell'],
        }
        self.write(self.root+'/commands.json', json.dumps(self.commands))
        self.write(self.root+'/launch.py', LAUNCHER)
        self.write(self.root+'/processes.py', PROCESSES)

    def runtime(self, agent):
        base = self.root+'/'+agent
        self.sftp.mkdir(base, 0o700)
        return {**self.config, 'tasks.base_dir': base,
                'tasks.init_script_path': '', 'tasks.init_script_text': '', 'agent.prompt': '',
                'agent.shell': shlex.join(['python3', self.root+'/launch.py', self.root, agent])+' "$PWD"'}

    def configure_claude_qwen(self):
        """Reuse the existing Qwen provider remotely; never return its credential."""
        script = '''import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]);q=json.loads((pathlib.Path.home()/'.qwen/settings.json').read_text())
model=q['model']['name'];p=next(p for p in q['modelProviders']['openai'] if p.get('id')==model)
key=q.get('env',{}).get(p.get('envKey')) or os.environ.get(p.get('envKey','')) or p.get('apiKey')
if not key:raise RuntimeError('Existing Qwen provider credential unavailable')
base=p['baseUrl'].rstrip('/').removesuffix('/v1')
directory=root/'claude-provider';directory.mkdir(mode=0o700)
settings={'env':{'ANTHROPIC_BASE_URL':base,'ANTHROPIC_AUTH_TOKEN':key,'ANTHROPIC_MODEL':model,
 'ANTHROPIC_DEFAULT_SONNET_MODEL':model,'ANTHROPIC_DEFAULT_OPUS_MODEL':model,
 'ANTHROPIC_DEFAULT_HAIKU_MODEL':model,'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}}
path=directory/'settings.json'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as handle:json.dump(settings,handle)
print(json.dumps({'base_url':base,'model':model,'source':'existing Qwen provider',
 'credential_returned_to_local_runner':False,'configuration_scope':'temporary test root'}))
'''
        return json.loads(self.command('python3 -c '+shlex.quote(script)+' '+shlex.quote(self.root)))

    def safe_failure(self, value):
        """Redact provider errors on the VM as the key never leaves that VM."""
        self.write(self.root+'/failure.txt', self.safe(value))
        script = '''import json,pathlib,sys
root=pathlib.Path(sys.argv[1]);text=(root/'failure.txt').read_text()
config=root/'claude-provider/settings.json'
if config.exists():
 for name,value in json.loads(config.read_text()).get('env',{}).items():
  if value and any(part in name for part in ['TOKEN','KEY']):text=text.replace(value,'[redacted]')
print(json.dumps(text[:1500]))
'''
        return json.loads(self.command('python3 -c '+shlex.quote(script)+' '+shlex.quote(self.root)))

    def settings_snapshot(self):
        script = '''import hashlib,json,pathlib
h=pathlib.Path.home()
files=['.qwen/settings.json','.codex/config.toml','.claude/settings.json','.claude/settings.local.json']
print(json.dumps({name:hashlib.sha256((h/name).read_bytes()).hexdigest() if (h/name).exists() else None for name in files}))
'''
        return json.loads(self.command('python3 -c '+shlex.quote(script)))

    def cache_snapshot(self):
        script = 'roots='+repr(CACHE_ROOTS)+'''\nimport json,pathlib
h=pathlib.Path.home()
print(json.dumps([str(p) for name in roots if (h/name).exists() for p in (h/name).rglob('*')]))
'''
        self.cache_before = json.loads(self.command('python3 -c '+shlex.quote(script)))

    def clean_caches(self, session_ids):
        # Only newly created files tied to this root/session can be removed.
        self.write(self.root+'/cache-cleanup.json', json.dumps({
            'before': self.cache_before, 'sessions': session_ids, 'root': self.root}))
        script = 'roots='+repr(CACHE_ROOTS)+'''\nimport json,pathlib,sys,sqlite3
data=json.loads(pathlib.Path(sys.argv[1]).read_text());h=pathlib.Path.home()
before=set(data['before']);root=data['root'];tokens=[root.encode()]+[s.encode() for s in data['sessions'] if s]
removed=[];directories=[]
for name in roots:
 base=h/name
 if not base.exists():continue
 for p in base.rglob('*'):
  if str(p) in before or p.is_symlink():continue
  if p.is_dir():directories.append(p);continue
  try:
   if root.replace('/','-') in str(p) or any(s in str(p) for s in data['sessions'] if s) or any(t in p.read_bytes() for t in tokens):
    p.unlink();removed.append(str(p))
  except OSError:pass
for p in sorted(directories,key=lambda p:len(p.parts),reverse=True):
 try:p.rmdir()
 except OSError:pass
deleted_threads=0
ids=[s for s in data['sessions'] if s]
for file in (h/'.codex').glob('*.sqlite'):
 if not ids:break
 db=sqlite3.connect(file,timeout=10)
 try:
  with db:
   # Exact test session IDs only. Schema/table identifiers come from SQLite itself.
   for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    table=row[0];quoted='"'+table.replace('"','""')+'"'
    cols={r[1] for r in db.execute('PRAGMA table_info('+quoted+')')}
    column='id' if table=='threads' else 'thread_id' if 'thread_id' in cols else None
    if column:
     changed=db.execute('DELETE FROM '+quoted+' WHERE "'+column+'" IN ('+','.join('?' for _ in ids)+')',ids).rowcount
     if table=='threads':deleted_threads+=changed
 finally:db.close()
index=h/'.codex/session_index.jsonl'
if index.exists():
 lines=index.read_text().splitlines(keepends=True);kept=[]
 for line in lines:
  try:owned=json.loads(line).get('id') in ids
  except ValueError:owned=False
  if not owned:kept.append(line)
 if len(kept)!=len(lines):index.write_text(''.join(kept))
print(json.dumps({'removed_cache_files':len(removed),'removed_codex_threads':deleted_threads}))
'''
        return json.loads(self.command('python3 -c '+shlex.quote(script)+' '+shlex.quote(self.root+'/cache-cleanup.json')))

    def cleanup(self):
        try:
            self.processes(stop=True)
            self.command('python3 -c '+shlex.quote('import shutil; shutil.rmtree('+repr(self.root)+')'))
            with suppress(FileNotFoundError):
                self.sftp.stat(self.root)
                raise AssertionError('Remote test directory still exists')
        finally:
            self.sftp.close()
            self.ssh.close()
