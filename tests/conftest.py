"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr("core.store.DB_PATH", db)
    monkeypatch.setattr("core.store.SEEN_JSON_PATH", tmp_path / "seen_urls.json")
    monkeypatch.setattr("core.store.MIGRATION_FLAG", tmp_path / ".seen_urls_migrated")
    from core import store
    store.init_db(db)
    return db
