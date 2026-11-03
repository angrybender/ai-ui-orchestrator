"""Local executor fixture for WAIT queue tests; never connects to SSH."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_launcher
import agent_store
import agent_worker
import board_store
from agent_consumer import stop_process


def configure_launcher() -> list:
    original_popen = agent_launcher.subprocess.Popen
    processes = []

    def local_worker(command, **kwargs):
        command = list(command)
        if len(command) > 1 and Path(command[1]).name == 'agent_worker.py':
            command[1] = str(Path(__file__).resolve())
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    agent_launcher.subprocess.Popen = local_worker
    return processes


def stop_test_workers(processes) -> None:
    """Stop only this isolated fixture's owners before removing its database."""
    with closing(board_store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        runs = db.execute('SELECT * FROM agent_runs').fetchall()
        # Invalidate pending handoffs before an unclaimed child can start.
        db.execute("UPDATE board_tasks SET status = 'BACKLOG', active_run_id = NULL")
    for process in processes:
        stop_process(process)
    for run in runs:
        pid = run['pid']
        if pid == os.getpid() or run['executor_scope'] != agent_store.executor_scope():
            continue
        if agent_store.process_identity(pid) != run['process_token']:
            continue
        if os.name == 'nt':
            from windows_process import terminate_process
            terminate_process(pid, run['process_token'])
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while agent_store.process_identity(pid) == run['process_token'] and time.monotonic() < deadline:
                time.sleep(0.02)
            if agent_store.process_identity(pid) == run['process_token']:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    args, _ = parser.parse_known_args()
    if args.database.resolve() == board_store.DATABASE_PATH.resolve():
        parser.error('An isolated test database is required.')
    agent_worker.settings_store.USER_SETTINGS_DIR = args.database.parent / 'settings'
    configure_launcher()

    def transport(task, attachments, config, on_session, on_log, on_started, on_message):
        on_started()
        on_session('test-session-' + task['task_id'])
        mode = task['description']
        if mode == 'init-error':
            raise agent_worker.InitError('Init script error: test failure')
        if mode == 'failure':
            raise agent_worker.RemoteAgentError('Test ACP failure')
        if mode == 'sigterm-at-finish':
            original_finish = agent_store.finish

            def stop_after_commit(*args, **kwargs):
                original_finish(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGTERM)

            agent_store.finish = stop_after_commit
            raise agent_worker.RemoteAgentError('Test ACP failure before SIGTERM')
        if mode == 'timeout':
            raise TimeoutError('Test deadline expired')
        if mode == 'uncertain':
            raise agent_worker.RemoteAgentError('Test remote stop unconfirmed', uncertain=True)
        if mode == 'refusal':
            return {'stopReason': 'refusal'}
        if mode == 'hold':
            release = args.database.parent / ('release-' + task['task_id'])
            deadline = time.monotonic() + 30
            while not release.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Test did not release executor')
                time.sleep(0.02)
        on_message('Test task completed')
        return {'stopReason': 'end_turn'}

    agent_worker.run_remote = transport
    return agent_worker.main()


if __name__ == '__main__':
    raise SystemExit(main())
