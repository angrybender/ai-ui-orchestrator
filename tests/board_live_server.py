"""Real, isolated FastAPI fixture and cross-process agent operations for board_live.js."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_store
import board_store


def configure(directory: Path) -> None:
    board_store.DATABASE_PATH = directory / "app.db"
    board_store.FILES_DIR = directory / "files"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--operation", choices=("claim", "success", "failure"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.operation:
        # Only accept an existing database owned by this fixture, never /app/data.
        directory = args.directory.resolve()
        if not directory.name.startswith("board-live-") or not (directory / "fixture-owned").is_file():
            parser.error("Refusing a database not owned by the live-board fixture")
        configure(directory)
        if args.operation == "claim":
            result = agent_store.claim(300)
            if result is None:
                raise RuntimeError("No task was claimed")
        else:
            agent_store.finish(args.run_id, "Live test agent failure" if args.operation == "failure" else None)
            result = {"finished": args.run_id}
        print(json.dumps(result), flush=True)
        return

    import main as application
    import uvicorn

    with TemporaryDirectory(prefix="board-live-") as temporary:
        directory = Path(temporary)
        (directory / "fixture-owned").touch()
        configure(directory)
        application.DATA_DIR = directory
        application.DATABASE_PATH = board_store.DATABASE_PATH
        application.init_database()  # Includes agent_store, before any requests.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            print(json.dumps({"url": f"http://127.0.0.1:{port}", "directory": temporary}), flush=True)
            server = uvicorn.Server(uvicorn.Config(application.app, log_level="warning", timeout_graceful_shutdown=2))

            def stop_on_stdin() -> None:
                sys.stdin.readline()
                server.should_exit = True

            threading.Thread(target=stop_on_stdin, daemon=True).start()
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
