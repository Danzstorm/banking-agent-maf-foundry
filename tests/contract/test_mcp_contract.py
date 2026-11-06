"""Contract tests: real server process, official MCP client, streamable HTTP. No Azure."""

import asyncio
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from mcp_server import banking
from mcp_server.db import audit_log
from tests.conftest import A

pytestmark = pytest.mark.contract

TOOLS = {
    "get_balance",
    "list_transactions",
    "get_claim_status",
    "block_card",
    "open_claim",
    "escalate_to_human",
}


def run(url: str, headers: dict, calls):
    """Open one MCP session with `headers` and run `calls(client)`."""

    async def main():
        async with (
            httpx2.AsyncClient(headers=headers, timeout=10) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            return await calls(client)

    return asyncio.run(main())


def payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_lists_exactly_the_six_tools_without_customer_id(server):
    url, _ = server

    async def calls(client):
        return (await client.list_tools()).tools

    tools = run(url, {}, calls)
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        params = set(tool.input_schema.get("properties", {}))
        assert not {p for p in params if "customer" in p.lower() or "cliente" in p.lower()}
        assert tool.description


def test_get_balance_with_dev_headers(server):
    url, _ = server

    async def calls(client):
        return await client.call_tool("get_balance", {})

    data = payload(run(url, {"X-Dev-Customer-Id": str(A)}, calls))
    assert [c["numero"] for c in data["cuentas"]] == ["0001"]


def test_call_without_identity_is_refused_and_audited(server):
    url, engine = server

    async def calls(client):
        return await client.call_tool("get_balance", {})

    result = run(url, {}, calls)
    assert result.is_error
    assert "No autorizado" in result.content[0].text
    with engine.connect() as conn:
        last = conn.execute(select(audit_log).order_by(audit_log.c.id.desc())).first()
    assert (last.tool, last.cliente_id, last.resultado) == ("get_balance", None, "denegado")


def test_open_claim_twice_over_http_is_idempotent_and_audited(server):
    url, engine = server
    headers = {"X-Dev-Customer-Id": str(A), "X-Conversation-Id": "contract-conv-1"}
    args = {"motivo": "Cobro duplicado en compra 4111111111111111"}

    async def calls(client):
        return [await client.call_tool("open_claim", args) for _ in range(2)]

    first, second = run(url, headers, calls)
    assert payload(first) == payload(second)
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_log).where(audit_log.c.conversation_id == "contract-conv-1")
        ).all()
    assert [(r.tool, r.cliente_id, r.resultado) for r in rows] == [("open_claim", A, "ok")] * 2
    assert "4111111111111111" not in rows[0].args_json  # long digit runs are masked


def test_write_tool_requires_conversation_id(server):
    url, _ = server

    async def calls(client):
        return await client.call_tool("block_card", {"card_id": 1})

    result = run(url, {"X-Dev-Customer-Id": str(A)}, calls)
    assert result.is_error
    assert "X-Conversation-Id" in result.content[0].text


def test_schema_validation_rejects_bad_input_and_is_audited(server):
    url, engine = server

    async def calls(client):
        return await client.call_tool("list_transactions", {"date_from": "ayer", "date_to": "x"})

    assert run(url, {"X-Dev-Customer-Id": str(A)}, calls).is_error
    with engine.connect() as conn:
        last = conn.execute(select(audit_log).order_by(audit_log.c.id.desc())).first()
    assert (last.tool, last.cliente_id, last.resultado) == ("list_transactions", A, "error")


def test_healthz_answers_without_mcp_session(server):
    url, _ = server
    response = httpx2.get(url.removesuffix("/mcp") + "/healthz", timeout=5)
    assert response.status_code == 200
    assert response.text == "ok"


def test_escalate_categories_match_the_domain_list(server):
    url, _ = server

    async def calls(client):
        return (await client.list_tools()).tools

    tool = next(t for t in run(url, {}, calls) if t.name == "escalate_to_human")
    assert tuple(tool.input_schema["properties"]["categoria"]["enum"]) == (
        banking.ESCALATION_CATEGORIES
    )
