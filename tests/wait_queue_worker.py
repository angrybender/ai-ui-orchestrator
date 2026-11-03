"""Local executor fixture for WAIT queue tests; never connects to SSH."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_launcher
import agent_worker
import board_store


def configure_launcher() -> None:
    original_popen = agent_launcher.subprocess.Popen

    def local_worker(command, **kwargs):
        command = list(command)
        if len(command) > 1 and Path(command[1]).name == 'agent_worker.py':
            command[1] = str(Path(__file__).resolve())
        return original_popen(command, **kwargs)

    agent_launcher.subprocess.Popen = local_worker


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
