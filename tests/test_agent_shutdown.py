"""Give the remote adapter time to stop its own detached native children."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from agent_cancel_fixture import LocalSSH, alive
from agent_control import AgentControl


@pytest.mark.skipif(os.name == 'nt', reason='Remote POSIX process groups')
def test_term_waits_for_adapter_to_clean_detached_child(tmp_path):
    script = tmp_path/'adapter.py'
    script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
def stop(*_):
    time.sleep(.35)
    child.terminate()
    child.wait()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path('ready').write_text(str(child.pid))
time.sleep(60)
''')
    adapter = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path, start_new_session=True)
    child = None
    transport = LocalSSH()
    try:
        deadline = time.monotonic()+5
        while not (tmp_path/'ready').exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        child = int((tmp_path/'ready').read_text())
        assert os.getpgid(child) != adapter.pid
        control = AgentControl(transport, threading.Event(), lambda _: None)
        control.pid = adapter.pid
        assert control.stop()
        adapter.wait(timeout=5)
        assert not alive(child), 'SIGKILL preempted adapter cleanup and orphaned its native child'
    finally:
        if adapter.poll() is None:
            adapter.kill()
        adapter.wait(timeout=5)
        if child and alive(child):
            os.kill(child, signal.SIGKILL)
