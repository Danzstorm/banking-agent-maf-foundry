FROM python:3.12-slim

# mssql-python bundles the ODBC driver but needs these system libs on Debian.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so code changes reuse this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# One image, three apps: mcp-server (default CMD), agent-host (python -m agent_host.service)
# and productos-a2a (python -m agent_host.productos_a2a).
COPY mcp_server ./mcp_server
COPY agent_host ./agent_host

RUN useradd --create-home --uid 10001 app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
EXPOSE 8000

CMD ["python", "-m", "mcp_server"]
