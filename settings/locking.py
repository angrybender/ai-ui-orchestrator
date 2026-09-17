"""Cross-process coordination of saved configuration and executor snapshots."""
from contextlib import contextmanager


@contextmanager
def configuration_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.config.lock').open('a+b') as handle:
        import os
        if os.name == 'nt':
            import msvcrt
            handle.write(b'\0')
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
