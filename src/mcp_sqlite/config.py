"""Configuration management from environment variables."""

import os
from dataclasses import dataclass


@dataclass
class SqliteConfig:
    """SQLite connection configuration."""

    database_path: str
    readonly: bool


def get_config() -> SqliteConfig:
    """Load SQLite configuration from environment variables."""
    return SqliteConfig(
        database_path=os.environ.get("SQLITE_DB_PATH", "data.db"),
        readonly=os.environ.get("SQLITE_READONLY", "true").lower() in ("true", "1", "yes"),
    )
