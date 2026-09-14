from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading

from agent_consumer import BASE_DIR, positive_interval, process_options, stop_process

logger = logging.getLogger(__name__)


def serve(stop: threading.Event, host: str, port: int, interval: float) -> int:
    processes = []
    try:
        commands = [
            ('web', [sys.executable, '-m', 'uvicorn', 'main:app', '--host', host, '--port', str(port)]),
            ('consumer', [sys.executable, str(BASE_DIR / 'agent_consumer.py'), '--poll-interval', str(interval)]),
        ]
        for name, command in commands:
            if stop.is_set():
                return 0
            options = process_options()
            if name == 'web':
                options['env'] = {**os.environ, 'APP_WEB_HOST': host, 'APP_WEB_PORT': str(port)}
            process = subprocess.Popen(command, cwd=BASE_DIR, stdin=subprocess.DEVNULL, **options)
            processes.append((name, process))
        logger.info('Web server: http://%s:%s; consumer enabled.', host, port)
        while not stop.wait(0.2):
            for name, process in processes:
                if process.poll() is not None:
                    logger.error('%s exited unexpectedly; stopping application.', name)
                    return 1
        return 0
    except OSError:
        logger.error('Unable to start application process.')
        return 1
    finally:
        for name, process in reversed(processes):
            stop_process(process, timeout=15 if name == 'consumer' else 10)


def main() -> int:
    parser = argparse.ArgumentParser(description='Start the web server and Board task consumer.')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--poll-interval', type=positive_interval, default=5.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    import main as application

    application.init_database()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    return serve(stop, args.host, args.port, args.poll_interval)


if __name__ == '__main__':
    raise SystemExit(main())
