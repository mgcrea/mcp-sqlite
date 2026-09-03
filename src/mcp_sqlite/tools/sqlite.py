"""SQLite database tools for MCP."""

import asyncio
import os
import re
import sqlite3

from mcp.server.mcpserver import MCPServer
from mcp_policy_guard import UNDETERMINED, Guard, PolicyDenied, Resource, audit_call, guarded

from ..config import get_config
from ..sql_validation import ReadOnlyViolationError, validate_readonly_query
from ..table_extraction import TableExtractionError, extract_referenced_tables, normalize_table_name

# Timeout configuration (seconds)
CONNECT_TIMEOUT = 10

# Only allow safe table names to prevent injection in PRAGMA calls
_SAFE_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ── What this server submits as policy resources ─────────────────────────────
#
# Two kinds, on every call, and the pair is the whole decision:
#
#   path_prefix : the absolute path of the SQLite file this deployment is pointed at
#   sql_table   : each bare table name the call touches, lowercased
#
# **Why both.** SQLite table names are unqualified. There is no schema component, so `users`
# in an HR database and `users` in an analytics database are the same string to a matcher
# that works textually — which the platform's does. This is one image, deployed once per
# database file, so the *file path* is the only thing that distinguishes two deployments,
# and it is knowable before any call runs: it is a constant of the deployment, read from
# `SQLITE_DB_PATH`. Without it, a grant written for one deployment silently covers every
# other one that happens to have a table of the same name. `evaluate()` requires every
# submitted resource to be allowed, so submitting the two independently gets the conjunction
# for free: a caller must be allowed both this database and the tables named inside it, and
# a rule can still say "any table, but only in the reporting database".
#
# **What was rejected, and why:**
#
# * `sql_table` alone — the shape `mcp-postgres`/`mcp-mssql` use. Wrong here for the reason
#   above: those two qualify with a schema and a database the connection already names, and
#   SQLite has neither.
# * `path_prefix` alone. That is the granularity the tool already had — the connection —
#   restated as a policy resource. The rule somebody actually wants to write is "this agent
#   may read `orders` but not `customers`", and this cannot express it.
# * Folding the two into one value, `<path>:<table>` or `<db-stem>.<table>`, to imitate
#   `schema.table`. It invents a value format no rule author writes and no other server
#   here produces, and it collapses two independent decisions into one string — after which
#   "any table in this database" is no longer expressible.
# * `sql_schema`. SQLite has no schemas. `main` is a connection-level qualifier, not
#   something anybody authors a rule about, and `table_extraction` normalizes it away
#   precisely so it cannot be used to sidestep a rule written about the bare name.
# * `sql_column`, as `mcp-mssql` submits alongside its tables. Rejected as out of scope
#   rather than as wrong: it needs a column extractor and a per-table schema map, and it
#   exists there for a specific shape (personal fields sharing a join with data the caller
#   may legitimately read). The table set is the finest grain here today, not the finest
#   grain possible.
# * `[]`. There is plenty to enumerate; `[]` would be an oversight wearing a decision's
#   clothes.
#
#: Selector kind the platform's policy store uses for SQL tables. Unqualified here — see
#: `table_extraction` for why, and for the normalization both paths must agree on.
SQL_TABLE = "sql_table"

#: Selector kind for the database file itself. Matching is a case-insensitive glob with `*`,
#: so `path_prefix:/data/*` covers a whole mount while `path_prefix:/data/hr.db` names one
#: file.
PATH_PREFIX = "path_prefix"

# Constructed once at import and shared: it is thread-safe, and SQLite work runs in
# `asyncio.to_thread`. `server.py` reads `guard.config` to decide which transports to mount.
guard = Guard()


def database_resource() -> Resource:
    """This deployment's database file, as a policy resource.

    Absolute, so a relative `SQLITE_DB_PATH` cannot produce two spellings of one file
    depending on the working directory. Symlinks are deliberately *not* resolved: the value
    a rule author writes is the path in the deployment manifest, and `realpath` would hand
    the matcher something they never saw.
    """
    return Resource(PATH_PREFIX, os.path.abspath(get_config().database_path))


def _denial_message(denied: PolicyDenied) -> str:
    """What to tell the model when a call is refused.

    An outage is not a denial. `PolicyUnavailable` subclasses `PolicyDenied` so the
    fail-closed path cannot be forgotten, but saying "you do not have access to `users`"
    while the decision point is down sends the user to raise an access request for a
    permission they already hold — and tells the model to stop trying something that will
    work again in a minute.

    Only *tables* are named back, never the database path. The model already wrote the table
    names, so repeating them reveals nothing and stops it retrying the same query in a loop.
    It never named the file, which is a location on the pod's filesystem and no help to it.
    """
    if denied.is_outage:
        return "Error: authorization is temporarily unavailable. Retry shortly; this is not a permissions problem."
    tables = sorted(r.split(":", 1)[-1] for r in denied.resources if r.startswith(f"{SQL_TABLE}:"))
    if tables:
        return f"Error: You do not have access to {', '.join(tables)}."
    return "Error: You do not have permission to run this against this database."


def register_sqlite_tools(mcp: MCPServer) -> None:
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
        """Synchronous SQLite query execution.

        Order matters and is deliberate:

          1. `validate_readonly_query` — a deny-list over tokens, when the server is
             read-only. Runs first because sqlglot parses `DELETE` perfectly happily;
             enumeration is not policy.
          2. `extract_referenced_tables` — the allow-list's input. Fails closed whenever the
             set of tables touched cannot be established.
          3. `guard.require` — the decision, made against the database file and the tables
             the model actually emitted. Anything an injected instruction persuaded the model
             to do is already in the query text by this point, which is exactly why the check
             lives here and not in the prompt.
          4. Execute.

        **No connection is opened until step 3 has passed.** A check that ran afterwards
        would have already read the data it exists to prevent reading.
        """
        config = get_config()

        with audit_call("sqlite_query", {"query": query}) as record:
            if config.readonly:
                try:
                    validate_readonly_query(query)
                except ReadOnlyViolationError as e:
                    record["decision"] = "deny"
                    record["reason"] = "read-only violation"
                    return f"Error: {e}"

            database = database_resource()
            try:
                tables = extract_referenced_tables(query)
            except TableExtractionError as e:
                # `UNDETERMINED`, not `[]` and not an early return.
                #
                # `[]` would be a lie: it means "this call touches nothing in particular",
                # and turning an extractor's failure into that claim converts the failure
                # into an allow.
                #
                # An early return — what `mcp-mssql` does here — is the other candidate, and
                # the two differ only where it matters most. `evaluate()` checks
                # `policy_enabled` *before* it inspects the sentinel, so with no PDP
                # configured `UNDETERMINED` short-circuits to allow and this tool behaves
                # exactly as it did before the guard was a dependency, while an early return
                # would start refusing every statement sqlglot cannot model. mcp-mssql can
                # afford that because all of its deployments are configured; this image has
                # none at all today, so every deployment it ever gets starts unconfigured —
                # the case where the two answers differ is the only case there is.
                record["reason"] = f"tables touched could not be determined: {e}"
                try:
                    decision = guard.require("sqlite_query", UNDETERMINED)
                except PolicyDenied as denied:
                    record["decision"] = "deny"
                    record["reason"] = denied.reason
                    # The extraction error, not the policy reason: it says what about the
                    # statement could not be analyzed, which is the part the model can act on.
                    return f"Error: {e}"
                # `decision.decision`, never a literal "allow". In shadow mode `require`
                # returns without raising while the recorded verdict is `deny`, and auditing
                # the real answer is the whole point of shadow mode.
                record["decision"] = decision.decision
            else:
                resources = [database] + [Resource(SQL_TABLE, table) for table in sorted(tables)]
                record["resources"] = [str(resource) for resource in resources]
                try:
                    decision = guard.require("sqlite_query", resources)
                except PolicyDenied as denied:
                    record["decision"] = "deny"
                    record["reason"] = denied.reason
                    return _denial_message(denied)
                record["decision"] = decision.decision

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
        """Synchronous SQLite list tables, scoped to what the caller may see.

        Two different questions, answered two different ways:

        * **The database file** is required outright. A caller who may not touch this
          database is told so plainly — there is no enumeration oracle to protect, because
          the file is a property of the deployment and the caller learns nothing from the
          refusal that the tool's existence did not already tell them.
        * **The table names** are filtered, never refused. A listing that said "3 tables
          hidden" would hand over the exact names of what cannot be reached, which is often
          the interesting half. A scoped caller simply sees a smaller database.
        """
        with audit_call("sqlite_list_tables", {}) as record:
            database = database_resource()
            record["resources"] = [str(database)]
            try:
                decision = guard.require("sqlite_list_tables", [database])
            except PolicyDenied as denied:
                record["decision"] = "deny"
                record["reason"] = denied.reason
                return _denial_message(denied)
            record["decision"] = decision.decision

            conn = _get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                tables = [row[0] for row in cur.fetchall()]
            finally:
                conn.close()

            try:
                visible = guard.filter_resources(
                    SQL_TABLE,
                    tables,
                    function_name="sqlite_list_tables",
                    key=normalize_table_name,
                )
            except PolicyDenied as denied:
                # Denied the *function*, not particular tables. Saying so names no table and
                # stops the model retrying a listing it will never be allowed to make.
                record["decision"] = "deny"
                record["reason"] = denied.reason
                return _denial_message(denied)

            if len(visible) != len(tables):
                record["decision"] = "partial"
            record["resources"] = [str(database)] + [f"{SQL_TABLE}:{normalize_table_name(name)}" for name in visible]

            if not visible:
                # Byte-identical to the response for a genuinely empty database.
                return "No tables found in database"

            return "Tables in database:\n" + "\n".join(f"  - {t}" for t in visible)

    def _sync_sqlite_describe_table(table_name: str) -> str:
        """Synchronous SQLite describe table, authorized before it queries.

        On denial this returns the **same string** the tool already returns for a table that
        does not exist. Distinguishing the two would turn every denial into a confirmation
        that the table is real — the enumeration oracle that filtering `sqlite_list_tables`
        exists to avoid, reintroduced one name at a time.
        """
        if not _SAFE_TABLE_NAME_RE.match(table_name):
            return (
                f"Error: Invalid table name '{table_name}'. Only alphanumeric characters and underscores are allowed."
            )

        not_found = f"Table '{table_name}' not found"

        with audit_call("sqlite_describe_table", {"table_name": table_name}) as record:
            resources = [database_resource(), Resource(SQL_TABLE, normalize_table_name(table_name))]
            record["resources"] = [str(resource) for resource in resources]
            try:
                decision = guard.require("sqlite_describe_table", resources)
            except PolicyDenied as denied:
                # The real reason goes to the audit trail; the model is told nothing.
                record["decision"] = "deny"
                record["reason"] = denied.reason
                # An outage is the one denial worth distinguishing here, and it is safe to:
                # the answer does not depend on the table or on the caller's grants, so it
                # leaks nothing, while folding it into "not found" would tell the model a
                # table it may well be allowed to read does not exist.
                return _denial_message(denied) if denied.is_outage else not_found
            record["decision"] = decision.decision

            conn = _get_connection()
            try:
                cur = conn.cursor()
                cur.execute(f"PRAGMA table_info({table_name})")  # noqa: S608
                columns = cur.fetchall()

                if not columns:
                    return not_found

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

    # `@guarded` sits under `@mcp.tool()` on every handler, so the SDK registers the wrapper.
    #
    # **It is not decoration.** On SDK 1.x an MCP session is opened by whoever sent
    # `initialize` and every later message is dispatched inside the task that spawned with
    # it, so a principal bound only by the ASGI middleware stays the session opener's for
    # the life of the session: two users sharing a session means the second one's query is
    # authorized against the first one's grants and the audit row names the wrong person.
    # This server is on 2.x, where the SDK dispatches each message in its own task and the
    # decorator finds no ambient per-message request — so it leaves the middleware's already
    # correct binding untouched and costs nothing. It stays because it is the one part of
    # this wiring a test can assert on (`is_guarded`), and forgetting it on a handler added
    # later is a mistake that reviews cleanly and passes every single-user test.
    #
    # `asyncio.to_thread` copies the context, so the bound principal travels into the worker
    # thread where `guard.require` is actually called.
    @mcp.tool(description=query_desc)
    @guarded
    async def sqlite_query(query: str) -> str:
        """Execute a SQL query on the SQLite database."""
        return await asyncio.to_thread(_sync_sqlite_query, query)

    @mcp.tool()
    @guarded
    async def sqlite_list_tables() -> str:
        """List all tables in the SQLite database.

        Returns:
            List of table names in the database.
        """
        return await asyncio.to_thread(_sync_sqlite_list_tables)

    @mcp.tool()
    @guarded
    async def sqlite_describe_table(table_name: str) -> str:
        """Get the schema/structure of a SQLite table.

        Args:
            table_name: Name of the table to describe.

        Returns:
            Table structure with column names, types, and constraints.
        """
        return await asyncio.to_thread(_sync_sqlite_describe_table, table_name)
