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
