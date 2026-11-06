"""The real handoff workflow, MCP tools and local MCP server, with scripted models. No Azure.

Checks the wiring, not the model: least privilege per agent, identity reaching the MCP server
through the workflow, approvals surfacing as workflow requests, and escalation tickets.
"""

import asyncio

import pytest
from agent_framework import Message
from sqlalchemy import select

from agent_host.agent import ToolCallRecorder, build_agent
from agent_host.config import ProductosSettings, Settings
from agent_host.productos import build_productos_agent
from agent_host.workflow import (
    Conversation,
    build_triage_agent,
    build_workflow,
    escalate_only_tool,
)
from mcp_server.db import audit_log, cards, escalation_tickets
from tests.conftest import CARD_A, A
from tests.contract.test_agent_loop import ScriptedClient, call

pytestmark = pytest.mark.contract

# The server fixture is module-scoped (one database): the denial test runs before the approval.

BANKING = {"get_balance", "list_transactions", "get_claim_status", "block_card", "open_claim"}


def converse(url, scripts: dict[str, ScriptedClient], text: str, approve: bool):
    settings = Settings("https://unused.example", "scripted", url, A)
    kb = ProductosSettings("https://unused.example", "scripted", "https://kb.example", "kb")
    recorders = {name: ToolCallRecorder() for name in scripts}
    requested: list[str] = []

    def decide(req) -> bool:
        requested.append(req.function_call.name)
        return approve

    async def main():
        triage = build_triage_agent(scripts["triage"], settings, middleware=[recorders["triage"]])
        productos = build_productos_agent(
            scripts["productos"],
            kb,
            search_credential=None,
            extra_tools=[escalate_only_tool(settings)],
            middleware=[recorders["productos"]],
        )
        operaciones = build_agent(
            scripts["operaciones"], settings, middleware=[recorders["operaciones"]]
        )
        try:
            conversation = Conversation(build_workflow(triage, productos, operaciones), A)
            return await conversation.send(text, decide), conversation
        finally:
            for agent in (triage, productos, operaciones):
                await agent.close()

    turn, conversation = asyncio.run(main())
    return turn, {k: r.names() for k, r in recorders.items()}, requested, conversation


def scripts(triage=(), productos=(), operaciones=()):
    return {
        "triage": ScriptedClient(*triage),
        "productos": ScriptedClient(*productos),
        "operaciones": ScriptedClient(*operaciones),
    }


def test_no_agent_but_operaciones_can_see_banking_tools(server):
    url, _ = server
    settings = Settings("https://unused.example", "scripted", url, A)
    only = escalate_only_tool(settings)
    full = build_agent(ScriptedClient(), settings).mcp_tools[0]
    assert set(only.allowed_tools) == {"escalate_to_human"}
    assert full.allowed_tools is None  # the whole server: banking tools + escalate

    kb = ProductosSettings("https://unused.example", "scripted", "https://kb.example", "kb")
    productos = build_productos_agent(ScriptedClient(), kb, None, extra_tools=[only])
    for tool in productos.mcp_tools:
        assert not BANKING & set(tool.allowed_tools or [])
        assert tool.allowed_tools is not None


def test_denied_write_in_the_workflow_never_runs(server):
    url, engine = server
    turn, calls, requested, _ = converse(
        url,
        scripts(
            triage=[call("handoff_to_operaciones")],
            operaciones=[call("block_card", card_id=CARD_A), Message("assistant", ["No se hizo."])],
        ),
        "Bloquea mi tarjeta",
        approve=False,
    )
    assert requested == ["block_card"]
    assert calls["operaciones"] == []
    assert turn.text.endswith("No se hizo.")
    with engine.connect() as conn:
        assert conn.scalar(select(cards.c.estado).where(cards.c.id == CARD_A)) == "activa"


def test_approval_for_block_card_flows_through_the_workflow(server):
    url, engine = server
    turn, calls, requested, conversation = converse(
        url,
        scripts(
            triage=[call("handoff_to_operaciones")],
            operaciones=[call("block_card", card_id=CARD_A), Message("assistant", ["Listo."])],
        ),
        "Bloquea mi tarjeta",
        approve=True,
    )
    assert turn.handoffs == [("triage", "operaciones")]
    assert requested == ["block_card"]
    assert turn.approvals == [("block_card", True)]
    assert calls == {
        "triage": ["handoff_to_operaciones"],
        "productos": [],
        "operaciones": ["block_card"],
    }
    assert turn.replies[-1] == ("operaciones", "Listo.")
    with engine.connect() as conn:
        assert conn.scalar(select(cards.c.estado).where(cards.c.id == CARD_A)) == "bloqueada"
        row = conn.execute(select(audit_log).where(audit_log.c.tool == "block_card")).one()
    assert (row.cliente_id, row.conversation_id) == (A, conversation.conversation_id)


def test_triage_escalates_without_approval_and_creates_a_ticket(server):
    url, engine = server
    escalate = call("escalate_to_human", categoria="fraude", motivo="Cargos que no reconozco")
    turn, calls, requested, conversation = converse(
        url,
        scripts(triage=[escalate, Message("assistant", ["Un asesor te contactará."])]),
        "Hay cargos que no reconozco",
        approve=False,
    )
    assert requested == []
    assert calls["triage"] == ["escalate_to_human"]
    assert turn.replies == [("triage", "Un asesor te contactará.")]
    with engine.connect() as conn:
        ticket = conn.execute(select(escalation_tickets)).one()
    assert (ticket.cliente_id, ticket.conversation_id) == (A, conversation.conversation_id)
    assert ticket.motivo == "[fraude] Cargos que no reconozco"


def test_triage_cannot_call_a_banking_tool(server):
    url, engine = server
    # The scripted "model" tries a tool Triage was not given.
    turn, calls, _, _ = converse(
        url,
        scripts(triage=[call("get_balance"), Message("assistant", ["No puedo."])]),
        "¿Cuál es mi saldo?",
        approve=True,
    )
    assert calls["triage"] == []
    with engine.connect() as conn:
        assert "get_balance" not in conn.execute(select(audit_log.c.tool)).scalars().all()
