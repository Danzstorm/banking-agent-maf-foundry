"""Phase 5 end to end: device code sign-in -> OBO with the Key Vault certificate -> deployed MCP.

INTERACTIVE: signs in by device code, so it never runs by default, in CI, or with `-m azure`.
Sign in as cliente1@<subdomain>.onmicrosoft.com (customer 1) when prompted:

    AUTH_MODE=entra ... uv run pytest -m interactive -s -v

Needs the AUTH_MODE=entra variables of agent_host (docs/guides/fase-5-identidad.md) and
`az login` in the workforce tenant (Key Vault and Foundry).
"""

import asyncio
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agent_host.agent import ToolCallRecorder, build_agent, build_foundry_client, chat_turn
from agent_host.agent import new_session as new_agent_session
from agent_host.config import ConfigError, load_settings

pytestmark = pytest.mark.interactive

try:
    SETTINGS = load_settings()
    assert SETTINGS.entra is not None, "AUTH_MODE=entra"
except (ConfigError, AssertionError) as exc:
    SETTINGS = None
    SKIP_REASON = str(exc)


@pytest.fixture(scope="module")
def identity():
    if SETTINGS is None:
        pytest.skip(SKIP_REASON)
    from azure.identity import DefaultAzureCredential

    from agent_host.identity import EntraIdentity, load_certificate_pem

    credential = DefaultAzureCredential()
    ident = EntraIdentity(SETTINGS.entra, lambda: load_certificate_pem(SETTINGS.entra, credential))
    print(f"\nSesión: {ident.sign_in()}")
    return ident


def call(token: str, tool: str, args: dict, extra_headers: dict | None = None):
    async def main():
        headers = {"Authorization": f"Bearer {token}", "X-Conversation-Id": "fase5-e2e"}
        async with (
            httpx2.AsyncClient(headers=headers | (extra_headers or {}), timeout=60) as http,
            Client(streamable_http_client(SETTINGS.mcp_url, http_client=http)) as client,
        ):
            return await client.call_tool(tool, args)

    return asyncio.run(main())


def own_cards(token: str, extra_headers: dict | None = None) -> set[int]:
    result = call(token, "get_balance", {}, extra_headers)
    assert not result.is_error, result.content
    data = json.loads(result.content[0].text)
    return {card["card_id"] for acc in data["cuentas"] for card in acc["tarjetas"]}


def test_obo_token_reaches_only_the_signed_in_customer(identity):
    token = identity.mcp_token()
    mine = own_cards(token)
    assert mine
    # A dev header naming another customer changes nothing in entra mode.
    assert own_cards(token, {"X-Dev-Customer-Id": "2"}) == mine


def test_cannot_block_another_customers_card(identity):
    token = identity.mcp_token()
    # Seed order: customer 2's first card follows customer 1's last one.
    other = max(own_cards(token)) + 1
    result = call(token, "block_card", {"card_id": other})
    assert result.is_error


def test_agent_answers_balance_through_obo(identity):
    from azure.identity.aio import DefaultAzureCredential

    recorder = ToolCallRecorder()

    async def main():
        async with DefaultAzureCredential() as credential:
            agent = build_agent(
                build_foundry_client(SETTINGS, credential), SETTINGS, middleware=[recorder]
            )
            try:
                session = new_agent_session(agent, identity.mcp_token)
                return await chat_turn(agent, session, "¿Cuál es mi saldo?", lambda req: False)
            finally:
                await agent.close()

    response = asyncio.run(main())
    print(f"\nAgente: {response.text}")
    assert "get_balance" in recorder.names()
