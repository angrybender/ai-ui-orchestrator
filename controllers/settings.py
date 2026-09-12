from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from settings import settings as settings_store

router = APIRouter()
templates = Jinja2Templates(directory=settings_store.BASE_DIR / "templates")


class SettingsPayload(BaseModel):
    values: dict[str, Any]


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, section: str | None = None):
    sections = settings_store.load_sections()
    if not sections:
        raise HTTPException(status_code=404, detail="No settings sections configured")
    selected = next((item for item in sections if item["key"] == section), sections[0])
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active": "settings",
            "sections": sections,
            "selected": selected,
            "values": settings_store.load_values(selected),
        },
    )


@router.post("/api/settings/{section_key}")
def save_settings(section_key: str, payload: SettingsPayload):
    section = next((item for item in settings_store.load_sections() if item["key"] == section_key), None)
    if section is None:
        raise HTTPException(status_code=404, detail="Settings section not found")
    try:
        values = settings_store.validate_values(section, payload.values)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=error.args[0]) from error
    settings_store.save_values(section_key, values)
    return {"ok": True, "values": values}
