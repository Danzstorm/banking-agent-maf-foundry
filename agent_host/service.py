"""Agent host as an HTTP service (Phase 6). Stateless replicas, durable conversations.

POST /chat   {conversation_id?, message?, approvals?: [{request_id, approved}]}
             -> {conversation_id, text, replies, pending_approvals: [{request_id, tool, args}]}
GET  /healthz  liveness: the process answers. No dependencies.
GET  /readyz   readiness: the host is built. No SQL query on purpose: a probe every few
               seconds would keep the free-offer database from auto-pausing.

AUTH_MODE=entra: `Authorization: Bearer <token for api://<agent-api>/access_as_user>`, validated
with the same code as the MCP server; the host then does OBO to the MCP API (ADR-009).
AUTH_MODE=dev: the customer is DEV_CUSTOMER_ID (local development and tests only).

Every request rebuilds the agents and the workflow and restores the conversation from its
checkpoint in `sesiones` (ADR-013), so any replica can serve any turn, including approving a
write that another replica proposed before it died.

Run locally: uv run python -m agent_host.service   (AGENT_BIND, AGENT_PORT, DATABASE_URL)
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agent_framework import Agent, WorkflowRunResult
from agent_framework.orchestrations import HandoffAgentUserRequest
from sqlalchemy import Engine
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_host.agent import CONVERSATION_KEY, CUSTOMER_KEY, Identity, TokenProvider
from agent_host.cli import _ignore_mcp_close_noise, reply_text
from agent_host.config import Settings
from agent_host.sessions import (
    SqlCheckpointStorage,
    customer_by_oid,
    owner,
    pending,
    sessions_engine,
)
from agent_host.workflow import build_workflow
from mcp_server.auth import AuthError, validate_jwt

log = logging.getLogger("agent_host.service")

AGENT_SCOPE = "access_as_user"
MAX_BODY_BYTES = 16_384
ESCALATION_OFFER = (
    "No pude completar tu consulta en este momento. Si quieres, un asesor de Banco Andino Demo "
    'puede ayudarte: escribe "quiero hablar con un asesor".'
)


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


@dataclass(frozen=True)
class Caller:
    customer_id: int
    identity: Identity  # what the MCP header_provider sends (dev id or OBO token provider)
    key: str  # rate-limit bucket


class TokenBucket:
    """Per-user rate limit: `burst` requests at once, refilled at `per_minute`.

    ponytail: in-memory, so the real ceiling is per_minute x replicas (2 at most here). Move
    the counters to a shared store if the limit must hold across replicas exactly.
    """

    def __init__(self, per_minute: float, burst: int, clock=time.monotonic) -> None:
        self.rate = per_minute / 60
        self.burst = burst
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True


@dataclass
class Host:
    engine: Engine
    authenticate: Callable[[Request], Awaitable[Caller]]
    build_participants: Callable[[], Sequence[Agent]]  # fresh triage, productos, operaciones
    limiter: TokenBucket = field(default_factory=lambda: TokenBucket(per_minute=20, burst=10))
    max_message_chars: int = 2000
    turn_timeout_s: float = 120
    status: Callable[[], dict[str, Any]] = dict  # extra /readyz details


def dev_authenticator(settings: Settings) -> Callable[[Request], Awaitable[Caller]]:
    customer = settings.dev_customer_id

    async def authenticate(_request: Request) -> Caller:
        return Caller(customer, customer, f"dev:{customer}")

    return authenticate


def entra_authenticator(settings: Settings, engine: Engine, obo) -> Callable:
    entra = settings.entra

    async def authenticate(request: Request) -> Caller:
        headers = {k.lower(): v for k, v in request.headers.items()}
        claims = await run_in_threadpool(  # JWKS refresh is blocking HTTP
            lambda: validate_jwt(
                headers,
                tenant_id=entra.tenant_id,
                issuer=entra.issuer,
                audience=entra.agent_api_client_id,
                jwks_uri=entra.jwks_uri,
                scope=AGENT_SCOPE,
            )
        )
        customer = await run_in_threadpool(customer_by_oid, engine, claims["oid"])
        if customer is None:
            raise AuthError()
        user_token = headers["authorization"].split(" ", 1)[1].strip()
        identity = TokenProvider(lambda: obo.mcp_token(user_token))
        return Caller(customer, identity, f"oid:{claims['oid']}")

    return authenticate


def _approval_requests(waiting: dict[str, Any]) -> dict[str, Any]:
    return {
        rid: data
        for rid, data in waiting.items()
        if getattr(data, "type", None) == "function_approval_request"
    }


async def advance(workflow, storage: SqlCheckpointStorage, body: dict, kwargs: dict):
    """Run one turn of the conversation from its latest checkpoint."""
    latest = await storage.get_latest()
    waiting = pending(latest)
    run = {"checkpoint_storage": storage, "function_invocation_kwargs": kwargs}
    if latest is not None and not waiting:
        # The replica that ran the previous turn died mid-superstep (for example, while an
        # approved write was executing). Finish that turn first; writes are idempotent (ADR-007).
        return await workflow.run(checkpoint_id=latest.checkpoint_id, **run)
    approvals, message = body.get("approvals"), body.get("message")
    asked = _approval_requests(waiting)
    if approvals:
        answers = {a["request_id"]: a["approved"] for a in approvals}
        if set(answers) != set(asked):
            raise HttpError(409, "Las aprobaciones no coinciden con las pendientes.")
        responses = {
            rid: asked[rid].to_function_approval_response(ok) for rid, ok in answers.items()
        }
    elif asked:
        raise HttpError(409, "Primero responde las aprobaciones pendientes.")
    elif latest is None:
        return await workflow.run(message, **run)
    else:
        user_request = next(
            rid for rid, data in waiting.items() if isinstance(data, HandoffAgentUserRequest)
        )
        responses = {user_request: HandoffAgentUserRequest.create_response(message)}
    return await workflow.run(checkpoint_id=latest.checkpoint_id, responses=responses, **run)


def _parse(raw: bytes, max_chars: int) -> dict:
    if len(raw) > MAX_BODY_BYTES:
        raise HttpError(413, "Solicitud demasiado grande.")
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HttpError(400, "JSON inválido.") from exc
    if not isinstance(body, dict):
        raise HttpError(400, "JSON inválido.")
    conversation_id, message, approvals = (
        body.get("conversation_id"),
        body.get("message"),
        body.get("approvals"),
    )
    if conversation_id is not None and (
        not isinstance(conversation_id, str) or not 0 < len(conversation_id) <= 100
    ):
        raise HttpError(400, "conversation_id inválido.")
    if (message is None) == (not approvals):
        raise HttpError(400, "Envía un mensaje o aprobaciones, no ambos.")
    if message is not None:
        if not isinstance(message, str) or not message.strip():
            raise HttpError(400, "El mensaje está vacío.")
        if len(message) > max_chars:
            raise HttpError(413, f"El mensaje supera los {max_chars} caracteres.")
        body["message"] = message.strip()
    if approvals is not None and not (
        isinstance(approvals, list)
        and len(approvals) <= 10
        and all(
            isinstance(a, dict)
            and isinstance(a.get("request_id"), str)
            and isinstance(a.get("approved"), bool)
            for a in approvals
        )
    ):
        raise HttpError(400, "approvals debe ser una lista de {request_id, approved}.")
    return body


def _render(conversation_id: str, result: WorkflowRunResult) -> dict:
    replies = [
        {"agent": e.executor_id, "text": reply_text(e.executor_id, e.data.text)}
        for e in result
        if e.type == "output" and getattr(e.data, "text", "")
    ]
    approvals = []
    for event in result.get_request_info_events():
        if getattr(event.data, "type", None) == "function_approval_request":
            call = event.data.function_call
            approvals.append(
                {"request_id": event.request_id, "tool": call.name, "args": call.parse_arguments()}
            )
    return {
        "conversation_id": conversation_id,
        "text": "\n".join(r["text"] for r in replies),
        "replies": replies,
        "pending_approvals": approvals,
    }


def create_app(host: Host | None = None) -> Starlette:
    """`host=None` builds the production host (Foundry, MCP, SQL) at startup."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        asyncio.get_running_loop().set_exception_handler(_ignore_mcp_close_noise)
        if host is not None:
            app.state.host = host
            yield
            return
        async with production_host() as built:
            app.state.host = built
            yield

    async def chat(request: Request) -> JSONResponse:
        h: Host = request.app.state.host
        try:
            try:
                caller = await h.authenticate(request)
            except AuthError as exc:
                raise HttpError(401, "No autorizado.") from exc
            if not h.limiter.allow(caller.key):
                raise HttpError(429, "Demasiadas solicitudes. Intenta en un minuto.")
            body = _parse(await request.body(), h.max_message_chars)
            conversation_id = body.get("conversation_id")
            if conversation_id is None:
                if body.get("approvals"):
                    raise HttpError(400, "No hay conversación que aprobar.")
                conversation_id = uuid4().hex
            elif await run_in_threadpool(owner, h.engine, conversation_id) != caller.customer_id:
                raise HttpError(404, "Conversación no encontrada.")  # never reveal whose it is
            storage = SqlCheckpointStorage(h.engine, conversation_id, caller.customer_id)
            kwargs = {CUSTOMER_KEY: caller.identity, CONVERSATION_KEY: conversation_id}
            agents = h.build_participants()
            try:
                async with asyncio.timeout(h.turn_timeout_s):
                    result = await advance(build_workflow(*agents), storage, body, kwargs)
            except HttpError:
                raise
            except TimeoutError as exc:
                raise HttpError(504, "Tiempo agotado.", text=ESCALATION_OFFER) from exc
            except Exception as exc:  # model/MCP down after retries: degrade, don't hang
                log.exception("turn failed: conversation %s", conversation_id)
                raise HttpError(503, "Servicio no disponible.", text=ESCALATION_OFFER) from exc
            finally:
                for agent in agents:
                    with contextlib.suppress(Exception):
                        await agent.close()
            open_requests = {e.request_id for e in result.get_request_info_events()}
            saved = pending(storage.saved)
            if not open_requests <= set(saved):  # MAF only logs a failed checkpoint save
                raise HttpError(503, "No se pudo guardar la conversación.", text=ESCALATION_OFFER)
            return JSONResponse(_render(conversation_id, result))
        except HttpError as err:
            headers = {"Retry-After": "60"} if err.status == 429 else None
            if err.status == 401:
                headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
            return JSONResponse(err.body, status_code=err.status, headers=headers)

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> JSONResponse:
        h: Host | None = getattr(request.app.state, "host", None)
        if h is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ready", **h.status()})

    return Starlette(
        routes=[
            Route("/chat", chat, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/readyz", readyz),
        ],
        lifespan=lifespan,
    )


@contextlib.asynccontextmanager
async def production_host():
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    from agent_host.agent import build_agent, build_foundry_client
    from agent_host.config import load_productos_settings, load_settings
    from agent_host.identity import OboExchange, load_certificate_pem
    from agent_host.productos import KB_TOOL, build_productos_agent
    from agent_host.resilience import CircuitBreaker, ResilientTools
    from agent_host.workflow import build_triage_agent, escalate_only_tool

    settings = load_settings()
    kb_settings = load_productos_settings()
    engine = sessions_engine(os.environ.get("DATABASE_URL", "sqlite:///./data/banco.db"))
    sync_credential = SyncCredential()
    if settings.entra is None:
        authenticate = dev_authenticator(settings)
    else:
        entra = settings.entra
        obo = OboExchange(entra, lambda: load_certificate_pem(entra, sync_credential))
        authenticate = entra_authenticator(settings, engine, obo)
    breaker = CircuitBreaker()  # shared by every request of this replica
    async with DefaultAzureCredential() as credential:
        client = build_foundry_client(settings, credential)

        def participants() -> list[Agent]:
            tools = [ResilientTools()]
            return [
                build_triage_agent(client, settings, middleware=tools),
                build_productos_agent(
                    client,
                    kb_settings,
                    sync_credential,
                    extra_tools=[escalate_only_tool(settings)],
                    middleware=[ResilientTools(breaker, guarded=KB_TOOL)],
                ),
                build_agent(client, settings, middleware=tools),
            ]

        env = os.environ
        yield Host(
            engine=engine,
            authenticate=authenticate,
            build_participants=participants,
            limiter=TokenBucket(
                per_minute=float(env.get("RATE_LIMIT_PER_MINUTE", "20")),
                burst=int(env.get("RATE_LIMIT_BURST", "10")),
            ),
            max_message_chars=int(env.get("MAX_MESSAGE_CHARS", "2000")),
            status=lambda: {"knowledge_breaker": breaker.state},
        )
    engine.dispose()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    uvicorn.run(
        create_app(),
        host=os.environ.get("AGENT_BIND", "127.0.0.1"),
        port=int(os.environ.get("AGENT_PORT", "8080")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
