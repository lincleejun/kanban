import pytest

from kanban_core.sqlite_repo import SqliteTaskRepository


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
    from kanban_core import config as cfg

    importlib.reload(cfg)
    from kanban_core import api as api_mod
    from kanban_core import server as server_mod

    importlib.reload(api_mod)
    importlib.reload(server_mod)

    from fastapi.testclient import TestClient

    with TestClient(server_mod.app) as c:
        yield c
