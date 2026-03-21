"""Tests for SQLite configuration loading."""

import os

from mcp_sqlite.config import SqliteConfig, get_config


class TestSqliteConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
        monkeypatch.delenv("SQLITE_READONLY", raising=False)

        config = get_config()
        assert config.database_path == "data.db"
        assert config.readonly is True

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("SQLITE_DB_PATH", "/tmp/test.db")
        monkeypatch.setenv("SQLITE_READONLY", "false")

        config = get_config()
        assert config.database_path == "/tmp/test.db"
        assert config.readonly is False

    def test_readonly_truthy_values(self, monkeypatch):
        for val in ("true", "True", "TRUE", "1", "yes", "YES"):
            monkeypatch.setenv("SQLITE_READONLY", val)
            assert get_config().readonly is True

    def test_readonly_falsy_values(self, monkeypatch):
        for val in ("false", "False", "0", "no", "off", "anything"):
            monkeypatch.setenv("SQLITE_READONLY", val)
            assert get_config().readonly is False
