import pytest


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "conversations.sqlite3"))
