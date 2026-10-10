"""Atomic cleanup of local SQLite data, preserving its schema."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import board_store


def clear_database() -> None:
    with closing(board_store.connect()) as connection, connection:
        # Disable FK actions before starting the transaction: every table is
        # emptied, so neither cascades nor table ordering are needed.
        connection.execute('PRAGMA foreign_keys = OFF')
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute(
            "SELECT 1 FROM agent_runs WHERE state IN ('PREPARING', 'RUNNING') LIMIT 1"
        ).fetchone():
            raise ValueError('Stop active agent runs before clearing the database.')
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table in tables:
            name = table['name'].replace('"', '""')
            connection.execute(f'DELETE FROM "{name}"')
        connection.execute('DELETE FROM sqlite_sequence')
        # Stage attachments on the same filesystem so a failed move/commit
        # can restore files alongside the database rollback. Include leftovers
        # from earlier clears; preserve hidden service files and directories.
        with TemporaryDirectory(prefix='.clear-db-', dir=board_store.FILES_DIR) as temporary:
            moved = []
            try:
                for source in board_store.FILES_DIR.iterdir():
                    if source.name.startswith('.') or not (source.is_file() or source.is_symlink()):
                        continue
                    target = Path(temporary) / source.name
                    source.replace(target)
                    moved.append((source, target))
                connection.commit()
            except BaseException:
                for source, target in reversed(moved):
                    target.replace(source)
                raise
            # TemporaryDirectory removes the staged files before returning.
