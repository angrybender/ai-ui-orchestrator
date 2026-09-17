"""Remove expired session logs during application startup."""
import logging
import stat
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_session_logs(directory: Path, max_age: int) -> None:
    cutoff = time.time() - max_age
    try:
        # Do not follow a redirected logs directory or recurse into subdirectories.
        if directory.is_symlink():
            return
        for path in directory.iterdir():
            if path.name.startswith('.'):
                continue
            try:
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Could not remove session log %s", path, exc_info=True)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not read session logs in %s", directory, exc_info=True)
