from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
UI_SETTINGS_DIR = BASE_DIR / "settings" / "ui"
USER_SETTINGS_DIR = BASE_DIR / "settings" / "user"
VALIDATION_SETTINGS_DIR = BASE_DIR / "settings" / "validation"


def load_sections(ui_dir: Path | None = None) -> list[dict[str, Any]]:
    ui_dir = ui_dir or UI_SETTINGS_DIR
    sections = []
    for path in sorted(ui_dir.glob("*.json"), key=lambda item: (item.stem != "remote_server", item.name)):
        with path.open(encoding="utf-8") as file:
            section = json.load(file)
        section["key"] = path.stem
        sections.append(section)
    return sections


def load_values(section: dict[str, Any], user_dir: Path | None = None, *, for_ui: bool = True) -> dict[str, Any]:
    user_dir = user_dir or USER_SETTINGS_DIR
    path = user_dir / f"{section['key']}.json"
    values: dict[str, Any] = {}
    if path.exists():
        with path.open(encoding="utf-8") as file:
            values = json.load(file)
    for group in section.get("groups", []):
        for field in group.get("fields", []):
            fallback = (False if field["type"] == "checkbox" else "") if for_ui else None
            if for_ui and field["type"] == "password":
                values[field["id"]] = ""
            else:
                values.setdefault(field["id"], field.get("default", fallback))
    return values


def save_values(section_key: str, values: dict[str, Any], user_dir: Path | None = None) -> None:
    user_dir = user_dir or USER_SETTINGS_DIR
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{section_key}.json"
    with path.open("w", encoding="utf-8") as file:
        json.dump(values, file, ensure_ascii=False, indent=2)
        file.write("\n")


def validate_section(section_key: str, values: dict[str, Any], validation_dir: Path | None = None) -> list[str]:
    validation_dir = validation_dir or VALIDATION_SETTINGS_DIR
    path = validation_dir / f"{section_key}.py"
    if not path.exists():
        return []
    spec = importlib.util.spec_from_file_location(f"settings.validation.{section_key}", path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate(values)


def validate_values(section: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    errors = []
    fields = {field["id"]: field for group in section.get("groups", []) for field in group.get("fields", [])}
    normalized: dict[str, Any] = {}
    for field_id, field in fields.items():
        value = values.get(field_id, field.get("default", ""))
        field_type = field["type"]
        if field_type == "checkbox":
            normalized[field_id] = bool(value)
            continue
        if field_type == "integer":
            try:
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError
                value = int(value)
                if value < (1 if field.get("required") else 0):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{field_id}: expected a valid integer")
                continue
        elif isinstance(value, str):
            value = value.strip()
        if field.get("required") and not value:
            errors.append(f"{field_id}: field is required")
        normalized[field_id] = value
    errors.extend(validate_section(section["key"], normalized))
    if errors:
        raise ValueError(errors)
    return normalized
