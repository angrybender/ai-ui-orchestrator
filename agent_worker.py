from __future__ import annotations

import argparse
import math
import os
import signal
from pathlib import Path

import agent_store
import board_store
from agent_remote import RemoteAgentError, run_remote
from settings.config import Config
from settings.locking import configuration_lock
from settings import settings as settings_store
from task_cycle import TaskCycle
from task_init import InitError

CONFIG_KEYS = (
    'remote_server.username', 'remote_server.host', 'remote_server.password',
    'remote_server.ssh_key', 'tasks.base_dir', 'agent.shell', 'agent.agent_timeout',
    'agent.prompt', 'agent.continue_session_arg',
    'tasks.init_script_path', 'tasks.init_script_text', 'tasks.init_script_timeout',
)


def execute() -> int:
    # Hold the configuration lock through the atomic claim: Save cannot split
    # this execution's snapshot or race its reservation.
    with configuration_lock(settings_store.USER_SETTINGS_DIR):
        config = {}
        config_error = False
        try:
            config = {key: Config.get(key) for key in CONFIG_KEYS}
            timeout = float(config['agent.agent_timeout'])
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
        except Exception:
            timeout = 7200
            config_error = True
        run = agent_store.claim(timeout)
    if run is None:
        return 0
    try:
        if config_error:
            raise RemoteAgentError('Invalid agent configuration.')
        task = board_store.get_task(run['task_id'])
        task['comment'] = run.get('comment')
        task['_cycle'] = TaskCycle(run, config)
        task['session_id'] = run.get('session_id')
        attachments = []
        for attachment in task['attachments']:
            path, name, _ = board_store.attachment_path(task['task_id'], attachment['id'])
            attachments.append((path, name))
        result = run_remote(
            task, attachments, config,
            on_session=lambda value: agent_store.session(run['id'], value),
            on_log=lambda text: agent_store.append_log(run['id'], text),
            on_started=lambda: agent_store.started(run['id']),
            on_message=lambda text: agent_store.message(run['id'], text),
        )
        stop_reason = result.get('stopReason')
        if stop_reason != 'end_turn':
            agent_store.finish(run['id'], f'Remote agent stopped: {stop_reason}.', stop_reason)
            return 1
        agent_store.finish(run['id'], stop_reason=stop_reason)
        return 0
    except InitError as error:
        agent_store.finish(run['id'], str(error), init_error=True, uncertain=error.uncertain)
        return 1
    except RemoteAgentError as error:
        agent_store.finish(run['id'], str(error), getattr(error, 'stop_reason', None))
        return 1
    except BaseException:
        agent_store.finish(run['id'], 'Agent executor failed.')
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Execute the first OPEN Board task.')
    parser.add_argument('--database', type=Path, default=board_store.DATABASE_PATH)
    parser.add_argument('--files', type=Path, default=board_store.FILES_DIR)
    args = parser.parse_args()
    board_store.DATABASE_PATH = args.database.resolve()
    board_store.FILES_DIR = args.files.resolve()
    board_store.init_database()
    agent_store.init_database()

    def terminate(signum, frame):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, terminate)
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, terminate)
    return execute()


if __name__ == '__main__':
    raise SystemExit(main())
