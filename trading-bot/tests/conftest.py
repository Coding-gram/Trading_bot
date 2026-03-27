import os
from pathlib import Path

import pytest


def _patch_if_present(monkeypatch: pytest.MonkeyPatch, module_name: str, attr_name: str, value):
    try:
        module = __import__(module_name, fromlist=[attr_name])
    except Exception:
        return
    if hasattr(module, attr_name):
        monkeypatch.setattr(module, attr_name, value)


@pytest.fixture(autouse=True)
def isolate_test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = str(tmp_path / "test_runtime.db")
    monkeypatch.setenv("DB_PATH", db_path)

    _patch_if_present(monkeypatch, "bot.config", "DB_PATH", db_path)
    _patch_if_present(monkeypatch, "bot.execution.paper_trade", "DB_PATH", db_path)
    _patch_if_present(monkeypatch, "bot.execution.live_trade", "DB_PATH", db_path)
