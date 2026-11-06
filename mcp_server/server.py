"""MCP server for Banco Andino Demo (streamable HTTP).

Tools never accept a customer id: it comes from `resolve_customer`. Every `tools/call`
is written to `auditoria` by `audit_middleware`, including rejected ones.
"""

import json
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import Engine, insert
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_server import banking
from mcp_server.auth import CONVERSATION_HEADER, AuthError, resolve_customer
from mcp_server.db import audit_log

WRITE_NOTE = (
    " OPERACIÓN DE ESCRITURA: requiere aprobación explícita del cliente antes de ejecutarse"
    " y el header X-Conversation-Id."
)

_LONG_DIGITS = re.compile(r"\d{8,}")


def _sanitize(args: Any) -> str:
    """Audit-safe copy of the arguments: masks long digit runs (card/account numbers)."""
    if not isinstance(args, dict):
        return "{}"
    clean = {str(k)[:50]: _LONG_DIGITS.sub("[redactado]", str(v))[:500] for k, v in args.items()}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


def build_server(engine: Engine) -> MCPServer:
    def customer_id(ctx: Context) -> int:
        try:
            return resolve_customer(ctx.headers, engine)
        except AuthError as exc:
            raise ToolError(str(exc)) from exc

    def conversation_id(ctx: Context) -> str:
        value = (ctx.headers or {}).get(CONVERSATION_HEADER, "").strip()
        if not value or len(value) > 100:
            raise ToolError("Falta el header X-Conversation-Id (máximo 100 caracteres).")
        return value

    def bank_call(fn, *args):
        try:
            return fn(engine, *args)
        except banking.BankError as exc:
            raise ToolError(str(exc)) from exc

    async def audit_middleware(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        headers = getattr(ctx.request, "headers", None)
        try:
            who = resolve_customer(headers, engine)
        except AuthError:
            who = None
        status = "denegado" if who is None else "error"
        try:
            result = await call_next(ctx)
            # The runner may hand back the serialized dict or the CallToolResult model.
            if isinstance(result, dict):
                is_error = result.get("isError", False)
            else:
                is_error = getattr(result, "is_error", True)
            if who is not None and not is_error:
                status = "ok"
            return result
        finally:
            row = {
                "cliente_id": who,
                "tool": str(params.get("name", ""))[:50],
                "args_json": _sanitize(params.get("arguments")),
                "resultado": status,
                "conversation_id": ((headers or {}).get(CONVERSATION_HEADER) or "")[:100] or None,
                "creado_en": datetime.now(UTC).replace(tzinfo=None),
            }

            def write():
                with engine.begin() as conn:
                    conn.execute(insert(audit_log).values(**row))

            await anyio.to_thread.run_sync(write)

    server = MCPServer(
        name="banco-andino-demo",
        instructions="Herramientas del core bancario simulado de Banco Andino Demo.",
        middleware=[audit_middleware],
    )

    # Liveness for Container Apps probes. Deliberately skips the database: probing SQL every
    # few seconds would keep Azure SQL serverless from ever auto-pausing.
    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

    @server.tool(
        description=(
            "Devuelve las cuentas del cliente autenticado con su saldo y moneda, y las tarjetas "
            "asociadas (card_id, últimos 4 dígitos y estado). Solo lectura."
        ),
        annotations=read_only,
    )
    def get_balance(ctx: Context) -> dict:
        return bank_call(banking.get_balance, customer_id(ctx))

    @server.tool(
        description=(
            "Lista los movimientos de las cuentas del cliente autenticado entre date_from y "
            f"date_to (formato AAAA-MM-DD, ambos incluidos, rango máximo {banking.MAX_RANGE_DAYS} "
            "días). Solo lectura."
        ),
        annotations=read_only,
    )
    def list_transactions(date_from: date, date_to: date, ctx: Context) -> dict:
        return bank_call(banking.list_transactions, customer_id(ctx), date_from, date_to)

    @server.tool(
        description="Consulta el estado de un reclamo del cliente autenticado. Solo lectura.",
        annotations=read_only,
    )
    def get_claim_status(
        claim_id: Annotated[int, Field(gt=0, description="Identificador del reclamo.")],
        ctx: Context,
    ) -> dict:
        return bank_call(banking.get_claim_status, customer_id(ctx), claim_id)

    @server.tool(
        description=(
            "Bloquea una tarjeta del cliente autenticado. Usa el card_id que devuelve "
            "get_balance. Si ya estaba bloqueada, responde lo mismo." + WRITE_NOTE
        ),
        annotations=write,
    )
    def block_card(
        card_id: Annotated[int, Field(gt=0, description="card_id de la tarjeta a bloquear.")],
        ctx: Context,
    ) -> dict:
        return bank_call(banking.block_card, customer_id(ctx), conversation_id(ctx), card_id)

    @server.tool(
        description=(
            "Abre un reclamo a nombre del cliente autenticado con el motivo indicado. "
            "Repetir la misma solicitud en la misma conversación no crea un reclamo nuevo."
            + WRITE_NOTE
        ),
        annotations=write,
    )
    def open_claim(
        motivo: Annotated[
            str,
            Field(
                min_length=banking.CLAIM_MIN_LEN,
                max_length=banking.CLAIM_MAX_LEN,
                description="Descripción breve del problema, en palabras del cliente.",
            ),
        ],
        ctx: Context,
    ) -> dict:
        return bank_call(banking.open_claim, customer_id(ctx), conversation_id(ctx), motivo)

    @server.tool(
        description=(
            "Deriva el caso del cliente autenticado a un asesor humano y crea un ticket. Úsala "
            "siempre ante fraude o cargos no reconocidos, fallecimiento del titular, reclamos "
            "legales, o cuando no hay una fuente confiable para responder. No requiere "
            "aprobación: es una solicitud de ayuda. Repetir la misma categoría en la misma "
            "conversación devuelve el ticket existente. Requiere el header X-Conversation-Id."
        ),
        annotations=write,
    )
    def escalate_to_human(
        categoria: Annotated[
            Literal["fraude", "fallecimiento", "legal", "sin_fuente", "otro"],
            Field(description="Tipo de caso."),
        ],
        motivo: Annotated[
            str,
            Field(
                min_length=banking.CLAIM_MIN_LEN,
                max_length=banking.ESCALATION_MAX_LEN,
                description="Resumen breve del caso, en palabras del cliente.",
            ),
        ],
        ctx: Context,
    ) -> dict:
        return bank_call(
            banking.escalate_to_human, customer_id(ctx), conversation_id(ctx), categoria, motivo
        )

    return server
