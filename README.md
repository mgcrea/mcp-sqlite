# mcp-sqlite

<!-- markdownlint-disable MD033 -->
<p align="center">
  <a href="https://github.com/mgcrea/mcp-sqlite/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/mgcrea/mcp-sqlite/ci.yml?style=for-the-badge&branch=main" alt="build status" />
  </a>
  <a href="https://ghcr.io/mgcrea/mcp-sqlite">
    <img src="https://img.shields.io/badge/ghcr.io-mgcrea%2Fmcp--sqlite-blue?style=for-the-badge" alt="docker image" />
  </a>
  <a href="https://github.com/mgcrea/mcp-sqlite">
    <img src="https://img.shields.io/badge/python-3.12+-blue?style=for-the-badge" alt="python version" />
  </a>
  <a href="https://github.com/mgcrea/mcp-sqlite">
    <img src="https://img.shields.io/github/license/mgcrea/mcp-sqlite?style=for-the-badge" alt="license" />
  </a>
</p>
<!-- markdownlint-enable MD033 -->

MCP tool server providing SQLite database access for AI agents.

## Tools

| Tool                    | Description                                |
| ----------------------- | ------------------------------------------ |
| `sqlite_query`          | Execute SQL queries (read-only by default) |
| `sqlite_list_tables`    | List all tables in the database            |
| `sqlite_describe_table` | Get the schema/structure of a table        |

## Configuration

| Variable          | Default   | Description                       |
| ----------------- | --------- | --------------------------------- |
| `SQLITE_DB_PATH`  | `data.db` | Path to the SQLite database file  |
| `SQLITE_READONLY` | `true`    | Enforce read-only mode            |
| `MCP_PORT`        | `8080`    | HTTP server port                  |
| `MCP_HOST`        | `0.0.0.0` | HTTP server host                  |
| `MCP_TRANSPORT`   | `http`    | Transport mode: `http` or `stdio` |

> Set `SQLITE_READONLY=false` to enable read/write mode (INSERT, UPDATE, DELETE, etc.).

## Access control

Per-caller authorization is provided by
[mcp-policy-guard](https://github.com/mgcrea/mcp-policy-guard), configured entirely through
the `MCP_*` variables the platform injects — see that package's README for the full table.
With none of them set the guard is inert: every caller is served unauthenticated and no
decision point is consulted, which is exactly how this tool behaved before access control
existed.

**Every call is authorized against two things: the database file, and the tables.** SQLite
table names are unqualified — there is no schema component — so `users` in one database and
`users` in another are the same string to a matcher that works textually. This is one image
deployed once per database file, so the file path is the only thing that tells two
deployments apart. Each call therefore submits a `path_prefix` resource carrying the absolute
`SQLITE_DB_PATH`, alongside a `sql_table` resource per table. Every submitted resource must
be allowed, so a caller needs both — and a rule can still say "any table, but only in the
reporting database".

**Queries are authorized against the tables they actually touch.** `sqlite_query` parses the
statement with sqlglot and enumerates every table it reads *or writes*, so a query joining
`orders` and `payroll` is a payroll read and fails as a whole — a join cannot launder access.
The check runs on the emitted SQL, after any prompt injection has had its say, which is why
it holds where a prompt instruction would not. When the set cannot be established with
certainty — `PRAGMA`, `ATTACH`, a table-valued function, an attached database, a parse
failure — the guard is asked with `UNDETERMINED` rather than an empty list: that denies
wherever policy is enforcing, and stays a genuine no-op where no policy exists at all. See
`src/mcp_sqlite/table_extraction.py` and its test suite.

**Discovery hides rather than refuses.** `sqlite_list_tables` silently omits tables the
caller may not see, and `sqlite_describe_table` returns the same *"not found"* string for a
denied table as for an absent one. Distinguishing the two would confirm which tables exist,
turning every denial into an enumeration oracle. The real reason is recorded in the audit
trail instead. `sqlite_query` is the deliberate exception: the model already named the table,
so an explicit denial reveals nothing and stops it retrying. The database *path* is never
echoed back either way.

Column-level rules (`sql_column`, as `mcp-mssql` submits) are not implemented here. The table
set is the finest grain this server offers today, not the finest grain possible.

## Endpoints

| Path       | Method | Description                             |
| ---------- | ------ | --------------------------------------- |
| `/healthz` | GET    | Health check for K8s probes             |
| `/`        | GET    | Server info (name, version, transports) |
| `/mcp`     | POST   | MCP Streamable-HTTP transport           |
| `/sse`     | GET    | MCP SSE transport (legacy, see below)   |

`/sse` is **not mounted** when `MCP_REQUIRE_AUTH=true`, and `/` stops advertising it. Under
SSE the long-lived connection that carried the `Authorization` header is not the request that
carries a tool call, so the caller cannot be attributed to the call — mounting it anyway
would leave a second, unauthenticated door onto the same tools.

## Development

```bash
make install   # Install dependencies
make server    # Run HTTP server
make stdio     # Run in stdio mode
make lint      # Lint code
make format    # Format code
make spec      # Run tests
```

## Usage with `.mcp.json`

<!-- prettier-ignore -->
```json
{
  "mcpServers": {
    "sqlite": {
      "type": "stdio",
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/path/to/data.db:/data/data.db:ro",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "SQLITE_DB_PATH=/data/data.db",
        "ghcr.io/mgcrea/mcp-sqlite"
      ]
    }
  }
}
```

## Docker

```bash
make docker-build
make docker-run
```
