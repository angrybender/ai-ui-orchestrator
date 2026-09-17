"""Isolated live web fixture with a real local ACP agent for Playwright."""
import argparse
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import uvicorn
import agent_store
import board_store
import main as application
from settings import settings as settings_store
from agent_cancel_fixture import Harness, alive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['cooperative', 'ignore'], default='cooperative')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='agent-cancel-') as temporary, pytest.MonkeyPatch.context() as patch:
        directory = Path(temporary)
        patch.setattr(board_store, 'DATABASE_PATH', directory / 'app.db')
        patch.setattr(board_store, 'FILES_DIR', directory / 'files')
        patch.setattr(application, 'DATABASE_PATH', directory / 'app.db')
        patch.setattr(application, 'DATA_DIR', directory)
        patch.setattr(settings_store, 'USER_SETTINGS_DIR', directory / 'settings')
        application.init_database()
        harness = Harness(directory, patch, args.mode)
        try:
            harness.start()
            @application.app.get('/fixture-state')
            def state():
                with board_store.connect() as db:
                    run = dict(db.execute('SELECT * FROM agent_runs').fetchone())
                marker = harness.cwd / 'cancel.received'
                return {'status': board_store.get_task('CANCEL-1')['status'],
                        'state': run['state'], 'stop_reason': run['stop_reason'],
                        'confirmed': bool(run['agent_stop_confirmed']),
                        'uncertain': bool(run['agent_uncertain']), 'ssh_closed': harness.ssh.closed,
                        'worker_alive': harness.worker.is_alive(), 'agent_alive': alive(run['agent_pid']),
                        'child_alive': alive(int((harness.cwd / 'child.pid').read_text())),
                        'cancel_received': marker.exists(),
                        'elapsed_since_cancel': time.monotonic() - float(marker.read_text()) if marker.exists() else None}
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen(128)
                server = uvicorn.Server(uvicorn.Config(application.app, log_level='warning', timeout_graceful_shutdown=2))
                def stop():
                    sys.stdin.readline()
                    server.should_exit = True
                threading.Thread(target=stop, daemon=True).start()
                print(json.dumps({'url': f'http://127.0.0.1:{listener.getsockname()[1]}', 'directory': temporary}), flush=True)
                server.run(sockets=[listener])
        finally:
            harness.cleanup()


if __name__ == '__main__':
    main()
