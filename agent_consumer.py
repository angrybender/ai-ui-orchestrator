from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def positive_interval(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Interval must be a positive finite number.')
    return number


def process_options() -> dict:
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def stop_process(process: subprocess.Popen, timeout: float = 10) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                process.terminate()
        else:
            process.terminate()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except ProcessLookupError:
        process.wait()


def consume(stop: threading.Event, interval: float) -> int:
    while not stop.is_set():
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, str(BASE_DIR / 'agent_worker.py')],
                cwd=BASE_DIR, stdin=subprocess.DEVNULL, **process_options(),
            )
            while process.poll() is None:
                if stop.wait(0.2):
                    break
            if process.poll() not in (None, 0):
                logger.warning('Agent worker failed; the queue will be checked again.')
        except OSError:
            logger.error('Unable to create agent worker process.')
        finally:
            if process is not None:
                stop_process(process)
        stop.wait(interval)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Continuously consume OPEN Board tasks.')
    parser.add_argument('--poll-interval', type=positive_interval, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    return consume(stop, args.poll_interval)


if __name__ == '__main__':
    raise SystemExit(main())
