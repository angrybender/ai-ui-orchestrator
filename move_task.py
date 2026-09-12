from __future__ import annotations

import argparse
import json
import sys

import board_store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move a Board task to a new status")
    parser.add_argument("task_id", help="Task identifier, for example PRJ-12")
    parser.add_argument("status", choices=(*board_store.STATUSES, "ERROR"), help="Destination status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    board_store.init_database()
    is_error = args.status == "ERROR"
    destination_status = "WAIT" if is_error else args.status
    position = sum(task["status"] == destination_status for task in board_store.list_tasks())
    try:
        tasks = board_store.move_task(
            args.task_id,
            destination_status,
            position,
            force=True,
            force_error=is_error,
        )
    except board_store.TaskNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except board_store.BoardError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    task = next(task for task in tasks if task["task_id"] == args.task_id)
    print(json.dumps(task, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
