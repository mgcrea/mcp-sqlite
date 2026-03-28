"""Tests for SQLite MCP tools — uses a real in-memory SQLite database."""

import os
import sqlite3
import tempfile

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_sqlite.tools.sqlite import register_sqlite_tools


@pytest.fixture()
def db_path():
    """Create a temporary SQLite database with test data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, active INTEGER DEFAULT 1)"
    )
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL)")
    conn.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
    conn.execute("INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')")
    conn.execute("INSERT INTO users (id, name, email, active) VALUES (3, 'Charlie', NULL, 0)")
    conn.execute("INSERT INTO orders (id, user_id, total) VALUES (1, 1, 99.99)")
    conn.execute("INSERT INTO orders (id, user_id, total) VALUES (2, 1, 25.50)")
    conn.execute("INSERT INTO orders (id, user_id, total) VALUES (3, 2, 150.00)")
    conn.commit()
    conn.close()

    yield path
    os.unlink(path)


@pytest.fixture()
def empty_db_path():
    """Create a temporary empty SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture()
def tools(db_path, monkeypatch):
    """Register tools against a temporary database in read-only mode."""
    monkeypatch.setenv("SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("SQLITE_READONLY", "true")

    mcp = FastMCP(name="test")
    register_sqlite_tools(mcp)

    # Extract the sync functions from the closure for direct testing
    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.name] = tool.fn
    return tool_map


@pytest.fixture()
def rw_tools(db_path, monkeypatch):
    """Register tools against a temporary database in read-write mode."""
    monkeypatch.setenv("SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("SQLITE_READONLY", "false")

    mcp = FastMCP(name="test")
    register_sqlite_tools(mcp)

    tool_map = {}
    for tool in mcp._tool_manager._tools.values():
        tool_map[tool.name] = tool.fn
    return tool_map


# ── sqlite_query ──────────────────────────────────────────────


class TestSqliteQuery:
    async def test_simple_select(self, tools):
        result = await tools["sqlite_query"](query="SELECT id, name FROM users ORDER BY id")
        assert "Alice" in result
        assert "Bob" in result
        assert "Charlie" in result

    async def test_select_with_where(self, tools):
        result = await tools["sqlite_query"](query="SELECT name FROM users WHERE active = 1")
        assert "Alice" in result
        assert "Bob" in result
        assert "Charlie" not in result

    async def test_select_with_join(self, tools):
        result = await tools["sqlite_query"](
            query="SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.total"
        )
        assert "Alice" in result
        assert "25.5" in result

    async def test_select_count(self, tools):
        result = await tools["sqlite_query"](query="SELECT COUNT(*) as cnt FROM users")
        assert "3" in result

    async def test_output_has_column_headers(self, tools):
        result = await tools["sqlite_query"](query="SELECT id, name FROM users LIMIT 1")
        lines = result.split("\n")
        assert "id" in lines[0]
        assert "name" in lines[0]
        # Second line is separator
        assert lines[1].startswith("-")

    async def test_readonly_blocks_insert(self, tools):
        result = await tools["sqlite_query"](query="INSERT INTO users (name) VALUES ('evil')")
        assert "Error" in result

    async def test_readonly_blocks_drop(self, tools):
        result = await tools["sqlite_query"](query="DROP TABLE users")
        assert "Error" in result

    async def test_readonly_blocks_delete(self, tools):
        result = await tools["sqlite_query"](query="DELETE FROM users")
        assert "Error" in result

    async def test_readonly_blocks_update(self, tools):
        result = await tools["sqlite_query"](query="UPDATE users SET name = 'evil' WHERE id = 1")
        assert "Error" in result

    async def test_empty_result(self, tools):
        result = await tools["sqlite_query"](query="SELECT * FROM users WHERE id = 999")
        lines = result.strip().split("\n")
        # Header + separator, no data rows
        assert len(lines) == 2


class TestSqliteQueryReadWrite:
    async def test_insert_when_rw(self, rw_tools, db_path):
        result = await rw_tools["sqlite_query"](
            query="INSERT INTO users (id, name, email) VALUES (4, 'Dave', 'd@e.com')"
        )
        assert "OK" in result
        assert "1 row(s) affected" in result

        # Verify it was actually committed
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name FROM users WHERE id = 4").fetchone()
        conn.close()
        assert row[0] == "Dave"

    async def test_update_when_rw(self, rw_tools):
        result = await rw_tools["sqlite_query"](query="UPDATE users SET active = 0 WHERE id = 1")
        assert "OK" in result

    async def test_create_table_when_rw(self, rw_tools):
        result = await rw_tools["sqlite_query"](query="CREATE TABLE new_table (id INTEGER PRIMARY KEY)")
        assert "OK" in result


# ── sqlite_list_tables ────────────────────────────────────────


class TestSqliteListTables:
    async def test_lists_tables(self, tools):
        result = await tools["sqlite_list_tables"]()
        assert "orders" in result
        assert "users" in result

    async def test_excludes_sqlite_internal_tables(self, tools):
        result = await tools["sqlite_list_tables"]()
        assert "sqlite_" not in result

    async def test_formatted_as_list(self, tools):
        result = await tools["sqlite_list_tables"]()
        assert "Tables in database:" in result
        assert "  - orders" in result
        assert "  - users" in result

    async def test_empty_database(self, empty_db_path, monkeypatch):
        monkeypatch.setenv("SQLITE_DB_PATH", empty_db_path)
        monkeypatch.setenv("SQLITE_READONLY", "false")

        mcp = FastMCP(name="test")
        register_sqlite_tools(mcp)

        tool_map = {}
        for tool in mcp._tool_manager._tools.values():
            tool_map[tool.name] = tool.fn

        result = await tool_map["sqlite_list_tables"]()
        assert result == "No tables found in database"


# ── sqlite_describe_table ─────────────────────────────────────


class TestSqliteDescribeTable:
    async def test_describe_users(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        assert "Table: users" in result
        assert "id" in result
        assert "name" in result
        assert "email" in result
        assert "active" in result

    async def test_column_headers(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        assert "Column | Type | Nullable | Default | Primary Key" in result

    async def test_primary_key_marked(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        # The id row should have YES in the Primary Key column
        for line in result.split("\n"):
            if line.startswith("id"):
                assert "YES" in line
                break

    async def test_nullable_column(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        for line in result.split("\n"):
            if line.startswith("email"):
                assert "YES" in line  # email is nullable
                break

    async def test_not_null_column(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        for line in result.split("\n"):
            if line.startswith("name"):
                assert "NO" in line  # name is NOT NULL
                break

    async def test_default_value(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users")
        for line in result.split("\n"):
            if line.startswith("active"):
                assert "1" in line  # DEFAULT 1
                break

    async def test_nonexistent_table(self, tools):
        result = await tools["sqlite_describe_table"](table_name="nonexistent")
        assert "not found" in result

    async def test_invalid_table_name_rejected(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users; DROP TABLE users")
        assert "Error" in result
        assert "Invalid table name" in result

    async def test_invalid_table_name_with_quotes(self, tools):
        result = await tools["sqlite_describe_table"](table_name="users'--")
        assert "Error" in result

    async def test_table_name_with_spaces_rejected(self, tools):
        result = await tools["sqlite_describe_table"](table_name="my table")
        assert "Error" in result
