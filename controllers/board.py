from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import board_store
from board_events import task_events
from agent_remote import RemoteAgentError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


class MovePayload(BaseModel):
    status: str
    position: int


def _error(error: Exception) -> HTTPException:
    if isinstance(error, board_store.TaskNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, board_store.TaskConflictError):
        return HTTPException(status_code=409, detail=error.args[0])
    return HTTPException(status_code=422, detail=error.args[0])


async def _files(uploaded) -> list[tuple[str, str | None, bytes]]:
    files = []
    for item in uploaded:
        if hasattr(item, "filename") and item.filename:
            files.append((item.filename, getattr(item, "content_type", None), await item.read()))
    return files


@router.get("/", response_class=HTMLResponse)
def board(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"active": "board"})


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(request: Request, task_id: str):
    try:
        task = board_store.get_task(task_id)
    except board_store.BoardError as error:
        raise _error(error) from error
    return templates.TemplateResponse(
        request=request,
        name="task.html",
        context={"active": "archive" if task["status"] == board_store.ARCHIVE_STATUS else "board", "task": task},
    )


@router.get("/api/board/events")
async def events(request: Request):
    return StreamingResponse(
        task_events(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/board/tasks")
def tasks():
    return {"tasks": board_store.list_tasks(), "statuses": board_store.STATUSES}


@router.get("/api/board/tasks/next-id")
def next_id():
    return {"task_id": board_store.next_task_id()}


@router.get("/api/board/tasks/{task_id}/chat")
def task_chat(task_id: str):
    try:
        return board_store.get_chat(task_id)
    except board_store.BoardError as error:
        raise _error(error) from error


class ChatPayload(BaseModel):
    comment: str


@router.post("/api/board/tasks/{task_id}/chat")
def add_chat(task_id: str, payload: ChatPayload):
    try:
        return board_store.add_user_comment(task_id, payload.comment)
    except board_store.BoardError as error:
        raise _error(error) from error

@router.get("/api/board/tasks/{task_id}")
def task(task_id: str):
    try:
        return board_store.get_task(task_id)
    except board_store.BoardError as error:
        raise _error(error) from error


@router.delete("/api/board/tasks/{task_id}", status_code=204)
def delete_task(task_id: str):
    try:
        board_store.delete_backlog_task(task_id)
    except RemoteAgentError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except board_store.BoardError as error:
        raise _error(error) from error


@router.post("/api/board/tasks", status_code=201)
async def create_task(request: Request):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        try:
            return board_store.create_task(
                str(body.get("task_id", "")),
                str(body.get("title", "")),
                str(body.get("description", "")),
                [],
            )
        except board_store.BoardError as error:
            raise _error(error) from error
    async with request.form() as form:
        try:
            return board_store.create_task(
                str(form.get("task_id", "")),
                str(form.get("title", "")),
                str(form.get("description", "")),
                await _files(form.getlist("files")),
            )
        except board_store.BoardError as error:
            raise _error(error) from error


@router.put("/api/board/tasks/{task_id}")
async def update_task(task_id: str, request: Request):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        removed = body.get("remove_attachment_ids", [])
        if not isinstance(removed, list) or not all(isinstance(item, int) for item in removed):
            raise HTTPException(status_code=422, detail={"files": "Invalid attachment selection"})
        try:
            return board_store.update_task(task_id, str(body.get("title", "")), str(body.get("description", "")), str(body.get("status", "BACKLOG")), removed, [])
        except board_store.BoardError as error:
            raise _error(error) from error
    async with request.form() as form:
        remove_attachment_ids = str(form.get("remove_attachment_ids", "[]"))
        try:
            removed = json.loads(remove_attachment_ids)
            if not isinstance(removed, list) or not all(isinstance(item, int) for item in removed):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(status_code=422, detail={"files": "Invalid attachment selection"}) from error
        try:
            return board_store.update_task(task_id, str(form.get("title", "")), str(form.get("description", "")), str(form.get("status", "BACKLOG")), removed, await _files(form.getlist("files")))
        except board_store.BoardError as error:
            raise _error(error) from error


@router.patch("/api/board/tasks/{task_id}/move")
def move_task(task_id: str, payload: MovePayload):
    try:
        return {"tasks": board_store.move_task(task_id, payload.status, payload.position)}
    except board_store.BoardError as error:
        raise _error(error) from error


@router.get("/api/board/tasks/{task_id}/attachments/{attachment_id}")
def attachment(task_id: str, attachment_id: int):
    try:
        path, original_name, content_type = board_store.attachment_path(task_id, attachment_id)
    except board_store.BoardError as error:
        raise _error(error) from error
    return FileResponse(path, filename=original_name, media_type=content_type)
