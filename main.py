from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from controllers.board import router as board_router
from controllers.archive import router as archive_router
from controllers.settings import router as settings_router
from controllers.agent import router as agent_router
from controllers.git_proxy import router as git_proxy_router

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "app.db"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from http_proxy import ProxyServers
    from log_cleanup import cleanup_session_logs
    from settings.config import Config

    init_database()
    cleanup_session_logs(DATA_DIR / "logs", Config.get("tasks.logs_max_age"))
    proxies = ProxyServers()
    app.state.http_proxies = proxies
    await proxies.start()
    try:
        yield
    finally:
        await proxies.stop()


app = FastAPI(title="AI UI Orchestrator", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(board_router)
app.include_router(archive_router)
app.include_router(settings_router)
app.include_router(agent_router)
app.include_router(git_proxy_router)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def init_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO app_meta(key, value) VALUES ('schema_version', '1')")
    import board_store
    import agent_store

    board_store.init_database()
    agent_store.init_database()


@app.get("/health")
def health():
    return {"status": "ok"}
