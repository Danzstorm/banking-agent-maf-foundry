"""Run the MCP server: `uv run python -m mcp_server` -> http://127.0.0.1:8000/mcp"""

import os

import uvicorn

from mcp_server.auth import require_bearer
from mcp_server.db import make_engine
from mcp_server.server import build_server

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    app = build_server(make_engine()).streamable_http_app(host=host)
    uvicorn.run(require_bearer(app), host=host, port=int(os.environ.get("MCP_PORT", "8000")))
