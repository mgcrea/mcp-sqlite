"""SQLite database tools for MCP."""

import asyncio
import re
import sqlite3

from mcp.server.fastmcp import FastMCP

from ..audit import audit_log
from ..config import get_config
from ..sql_validation import ReadOnlyViolationError, validate_readonly_query

# Timeout configuration (seconds)
CONNECT_TIMEOUT = 10

# Only allow safe table names to prevent injection in PRAGMA calls
_SAFE_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def register_sqlite_tools(mcp: FastMCP) -> None:
    """Register SQLite tools with the MCP server."""

    def _get_connection():
        """Create SQLite connection with read-only mode if configured."""
        config = get_config()
        if config.readonly:
            uri = f"file:{config.database_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=CONNECT_TIMEOUT)
        else:
            conn = sqlite3.connect(config.database_path, timeout=CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    def _sync_sqlite_query(query: str) -> str:
        """Synchronous SQLite query execution."""
        config = get_config()
        if config.readonly:
            try:
                validate_readonly_query(query)
            except ReadOnlyViolationError as e:
                return f"Error: {e}"

        with audit_log("sqlite_query", {"query": query}):
            conn = _get_connection()
            try:
                cur = conn.cursor()
                cur.execute(query)

                if cur.description is None:
                    # Non-SELECT statement (write/DDL) — commit and report rows affected
                    conn.commit()
                    return f"OK — {cur.rowcount} row(s) affected"

                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

                # Format output
                result_lines = [" | ".join(columns)]
                result_lines.append("-" * len(result_lines[0]))
                for row in rows:
                    result_lines.append(" | ".join(str(val) for val in row))

                return "\n".join(result_lines)
            finally:
                conn.close()

    def _sync_sqlite_list_tables() -> str:
        """Synchronous SQLite list tables."""
        with audit_log("sqlite_list_tables", {}):
            conn = _get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                tables = [row[0] for row in cur.fetchall()]

                if not tables:
                    return "No tables found in database"

                return "Tables in database:\n" + "\n".join(f"  - {t}" for t in tables)
            finally:
                conn.close()

    def _sync_sqlite_describe_table(table_name: str) -> str:
        """Synchronous SQLite describe table."""
        if not _SAFE_TABLE_NAME_RE.match(table_name):
            return f"Error: Invalid table name '{table_name}'. Only alphanumeric characters and underscores are allowed."

        with audit_log("sqlite_describe_table", {"table_name": table_name}):
            conn = _get_connection()
            try:
                cur = conn.cursor()
                cur.execute(f"PRAGMA table_info({table_name})")  # noqa: S608
                columns = cur.fetchall()

                if not columns:
                    return f"Table '{table_name}' not found"

                result_lines = [f"Table: {table_name}", ""]
                result_lines.append("Column | Type | Nullable | Default | Primary Key")
                result_lines.append("-" * 60)

                for col in columns:
                    # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
                    name = col[1]
                    dtype = col[2] or "TEXT"
                    nullable = "NO" if col[3] else "YES"
                    default = str(col[4]) if col[4] is not None else ""
                    pk = "YES" if col[5] else ""
                    result_lines.append(f"{name} | {dtype} | {nullable} | {default} | {pk}")

                return "\n".join(result_lines)
            finally:
                conn.close()

    config = get_config()
    if config.readonly:
        query_desc = "Execute a read-only SQL query on the SQLite database.\n\nArgs:\n    query: SQL SELECT query to execute. Only SELECT statements are allowed.\n\nReturns:\n    Query results as formatted text with column headers."
    else:
        query_desc = "Execute a SQL query on the SQLite database. Supports both read and write queries (SELECT, INSERT, UPDATE, DELETE).\n\nArgs:\n    query: SQL query to execute. SELECT returns rows; write statements return affected row count.\n\nReturns:\n    Query results as formatted text, or affected row count for write operations."

    @mcp.tool(description=query_desc)
    async def sqlite_query(query: str) -> str:
        """Execute a SQL query on the SQLite database."""
        return await asyncio.to_thread(_sync_sqlite_query, query)

    @mcp.tool()
    async def sqlite_list_tables() -> str:
        """List all tables in the SQLite database.

        Returns:
            List of table names in the database.
        """
        return await asyncio.to_thread(_sync_sqlite_list_tables)

    @mcp.tool()
    async def sqlite_describe_table(table_name: str) -> str:
        """Get the schema/structure of a SQLite table.

        Args:
            table_name: Name of the table to describe.

        Returns:
            Table structure with column names, types, and constraints.
        """
        return await asyncio.to_thread(_sync_sqlite_describe_table, table_name)
