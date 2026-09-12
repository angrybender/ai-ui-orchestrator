from __future__ import annotations

from math import ceil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import board_store
from agent_remote import RemoteAgentError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
PAGE_SIZE = 20


@router.get("/archive", response_class=HTMLResponse)
def archive(request: Request):
    return templates.TemplateResponse(request=request, name="archive.html", context={"active": "archive"})


@router.get("/api/archive/tasks")
def archive_tasks(page: int = Query(1, ge=1)):
    tasks, total = board_store.list_archive(page, PAGE_SIZE)
    return {"tasks": tasks, "page": page, "page_size": PAGE_SIZE, "total": total, "pages": ceil(total / PAGE_SIZE)}


@router.delete("/api/archive/tasks/{task_id}", status_code=204)
def delete_archive_task(task_id: str):
    try:
        board_store.delete_archived_task(task_id)
    except RemoteAgentError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except board_store.BoardError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/archive/tasks/{task_id}")
def archive_task(task_id: str):
    try:
        task = board_store.get_task(task_id)
    except board_store.BoardError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if task["status"] != board_store.ARCHIVE_STATUS:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
