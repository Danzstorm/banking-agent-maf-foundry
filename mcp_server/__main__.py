"""Run the MCP server: `uv run python -m mcp_server` -> http://127.0.0.1:8000/mcp"""

import os

from mcp_server.db import make_engine
from mcp_server.server import build_server

if __name__ == "__main__":
    build_server(make_engine()).run(
        "streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
