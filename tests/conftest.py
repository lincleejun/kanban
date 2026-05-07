import os
import tempfile

import pytest

from app.sqlite_repo import SqliteTaskRepository


@pytest.fixture
def repo(tmp_path):
    db = tmp_path / "test.db"
    return SqliteTaskRepository(str(db))


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("KANBAN_DB_PATH", str(db))
    # Re-import settings so env var takes effect.
    import importlib
    from app import config as cfg

    importlib.reload(cfg)
    from app import api as api_mod
    from app import main as main_mod

    importlib.reload(api_mod)
    importlib.reload(main_mod)

    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c
