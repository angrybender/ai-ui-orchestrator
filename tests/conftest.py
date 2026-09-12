import pytest

import board_store
import main
from settings import settings as settings_store


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DATABASE_PATH', tmp_path / 'app.db')
    monkeypatch.setattr(board_store, 'DATABASE_PATH', tmp_path / 'app.db')
    monkeypatch.setattr(board_store, 'FILES_DIR', tmp_path / 'files')
    monkeypatch.setattr(settings_store, 'USER_SETTINGS_DIR', tmp_path / 'settings')
