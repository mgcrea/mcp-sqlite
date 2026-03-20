# mcp-sqlite

MCP tool server — read-only SQLite access for AI agents.

## Tools

| Tool                    | Description                          |
| ----------------------- | ------------------------------------ |
| `sqlite_query`          | Execute a read-only SQL SELECT query |
| `sqlite_list_tables`    | List all tables in the database      |
| `sqlite_describe_table` | Get the schema/structure of a table  |

## Configuration

| Variable          | Default   | Description                       |
| ----------------- | --------- | --------------------------------- |
| `SQLITE_DB_PATH`  | `data.db` | Path to the SQLite database file  |
| `SQLITE_READONLY` | `true`    | Enforce read-only mode            |
| `MCP_PORT`        | `8080`    | HTTP server port                  |
| `MCP_HOST`        | `0.0.0.0` | HTTP server host                  |
| `MCP_TRANSPORT`   | `http`    | Transport mode: `http` or `stdio` |

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

## Docker

```bash
make docker-build
make docker-run
```
