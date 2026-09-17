from __future__ import annotations

from typing import Any

from settings import settings as settings_store


class Config:
    @staticmethod
    def snapshot(keys):
        from settings.locking import configuration_lock
        with configuration_lock(settings_store.USER_SETTINGS_DIR):
            return {key: Config.get(key) for key in keys}

    @staticmethod
    def get(key: str) -> Any:
        section_key, separator, field_id = key.partition(".")
        if not separator or not section_key or not field_id:
            return None
        section = next(
            (item for item in settings_store.load_sections() if item["key"] == section_key),
            None,
        )
        if section is None:
            return None
        return settings_store.load_values(section, for_ui=False).get(field_id)
