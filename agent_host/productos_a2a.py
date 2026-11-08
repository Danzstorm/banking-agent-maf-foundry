"""Productos agent as an A2A 1.0 server (Phase 7, ADR-015). Experimental: agent-framework-a2a
is beta.

GET  /.well-known/agent-card.json  public card: skill, JSON-RPC endpoint, bearer scheme.
POST /                             JSON-RPC 2.0, method `SendMessage`, header `A2A-Version: 1.0`.
                                   Needs an Entra token of the workforce tenant for the
                                   `productos-a2a` app with the role Productos.Invoke (only the
                                   agent host's managed identity holds it). Anything else: 401.
GET  /healthz                      liveness and readiness; no dependencies.

Same agent as the in-process one (knowledge base only, no customer data, no MCP banking tools),
so it needs no customer identity: that is why Productos is the remote one (ADR-015).

Run: uv run python -m agent_host.productos_a2a
env: FOUNDRY_PROJECT_ENDPOINT, SEARCH_ENDPOINT, A2A_PUBLIC_URL, A2A_TENANT_ID, A2A_AUDIENCE,
     A2A_BIND (127.0.0.1), A2A_PORT (8090)
"""

import os
from collections.abc import Callable, Mapping
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from agent_framework import SupportsAgentRun
from agent_framework.a2a import A2AExecutor
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from agent_host.config import ConfigError
from agent_host.productos import DESCRIPTION
from mcp_server.auth import AuthError, validate_jwt

INVOKE_ROLE = "Productos.Invoke"
SCHEME = "entra"
OPEN_PATHS = {AGENT_CARD_WELL_KNOWN_PATH, "/healthz"}

Validator = Callable[[Mapping[str, str]], Any]  # raises AuthError


def agent_card(url: str) -> AgentCard:
    return AgentCard(
        name="productos",
        description=DESCRIPTION,
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        security_schemes={
            SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="JWT",
                    description=f"Token Entra (managed identity) con el rol {INVOKE_ROLE}.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={SCHEME: StringList()})],
        skills=[
            AgentSkill(
                id="productos",
                name="Productos de Banco Andino Demo",
                description=(
                    "Responde con citas sobre productos, tarifas, requisitos y procesos, solo "
                    'desde la base de conocimiento. La respuesta es JSON {"answer", "sources"}.'
                ),
                tags=["banca", "productos", "tarifas"],
                examples=["¿Cuál es la comisión de mantenimiento de la cuenta corriente?"],
            )
        ],
    )


class RequireToken:
    """ASGI gate: every path but the card and /healthz needs a token `validate` accepts."""

    def __init__(self, app, validate: Validator) -> None:
        self.app = app
        self.validate = validate

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] not in OPEN_PATHS:
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            try:
                await run_in_threadpool(self.validate, headers)  # JWKS refresh is blocking HTTP
            except AuthError:
                response = PlainTextResponse(
                    "No autorizado.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def entra_validator(tenant_id: str, audience: str) -> Validator:
    """App-only tokens of the workforce tenant for the productos-a2a app, role Productos.Invoke.

    Both token versions are accepted for the same app (v2 is configured; v1 differs only in
    `iss` and `aud` shape), so a manifest change does not lock the agent host out.
    """
    login = f"https://login.microsoftonline.com/{tenant_id}"
    issuers = [f"{login}/v2.0", f"https://sts.windows.net/{tenant_id}/"]
    audiences = [audience, f"api://{audience}"]

    def validate(headers: Mapping[str, str]) -> dict[str, Any]:
        return validate_jwt(
            headers,
            tenant_id=tenant_id,
            issuer=issuers,
            audience=audiences,
            jwks_uri=f"{login}/discovery/v2.0/keys",
            role=INVOKE_ROLE,
        )

    return validate


def create_app(agent: SupportsAgentRun, *, url: str, validate: Validator) -> Starlette:
    card = agent_card(url)
    handler = DefaultRequestHandler(
        agent_executor=A2AExecutor(agent),
        # ponytail: tasks stay in memory until the replica stops (scale to zero clears them).
        # Use a database task store if tasks must survive restarts or be polled later.
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    async def healthz(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
            Route("/healthz", healthz),
        ],
        middleware=[Middleware(RequireToken, validate=validate)],
    )


def main() -> None:
    import logging

    import uvicorn
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    from agent_host.agent import build_foundry_client
    from agent_host.config import load_productos_settings
    from agent_host.productos import KB_TOOL, build_productos_agent
    from agent_host.resilience import CircuitBreaker, ResilientTools
    from agent_host.telemetry import configure_telemetry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    env = os.environ
    missing = [n for n in ("A2A_PUBLIC_URL", "A2A_TENANT_ID", "A2A_AUDIENCE") if not env.get(n)]
    if missing:
        raise ConfigError(f"Faltan variables de entorno: {', '.join(missing)}.")
    settings = load_productos_settings()
    # Never closed: the credential lives as long as the process.
    client = build_foundry_client(settings, DefaultAzureCredential())
    agent = build_productos_agent(
        client,
        settings,
        SyncCredential(),
        middleware=[ResilientTools(CircuitBreaker(), guarded=KB_TOOL)],
    )
    app = create_app(
        agent,
        url=env["A2A_PUBLIC_URL"],
        validate=entra_validator(env["A2A_TENANT_ID"], env["A2A_AUDIENCE"]),
    )
    configure_telemetry("productos-a2a", app)
    uvicorn.run(
        app,
        host=env.get("A2A_BIND", "127.0.0.1"),
        port=int(env.get("A2A_PORT", "8090")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
