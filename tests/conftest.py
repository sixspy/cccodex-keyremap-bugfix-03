
import pytest

import app as app_module
from app import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    """每个测试独立 SQLite 库，库路径仍落在题目 data 目录语义之下（tmp 替身）。"""
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "t.db")
    init_db()
    yield
