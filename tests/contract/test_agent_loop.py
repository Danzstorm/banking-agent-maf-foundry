"""The real MAF loop, MCP tool and local MCP server, with a scripted model. No Azure.

The scripted client stands in for gpt-5-mini so the security properties are deterministic:
identity headers come from the session, writes wait for approval, a denial executes nothing.
"""

import asyncio
import json

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message
from sqlalchemy import select

from agent_host.agent import ToolCallRecorder, build_agent, chat_turn, new_session
from agent_host.config import Settings
from mcp_server.db import audit_log, cards
from tests.conftest import CARD_A, A, B

pytestmark = pytest.mark.contract


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Returns the scripted assistant messages in order, one per model call."""

    def __init__(self, *script: Message) -> None:
        super().__init__()
        self.script = list(script)

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        async def respond() -> ChatResponse:
            return ChatResponse(messages=[self.script.pop(0)])

        return respond()


def call(name: str, **arguments) -> Message:
    return Message(
        "assistant",
        [Content.from_function_call(call_id=f"call-{name}", name=name, arguments=arguments)],
    )


def converse(url: str, client: ScriptedClient, text: str, approve: bool):
    settings = Settings("https://unused.example", "scripted", url, A)
    recorder = ToolCallRecorder()
    requests: list[str] = []

    def decide(req: Content) -> bool:
        requests.append(req.function_call.name)
        return approve

    async def main():
        agent = build_agent(client, settings, middleware=[recorder])
        try:
            session = new_session(agent, A)
            response = await chat_turn(agent, session, text, decide)
            return response, session
        finally:
            await agent.close()

    response, session = asyncio.run(main())
    return response, recorder, requests, session


def tool_output(response) -> dict:
    """The MCP result the loop fed back to the model."""
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_result":
                return json.loads(content.result if isinstance(content.result, str) else "{}")
    raise AssertionError("no function_result in the response")


def test_identity_comes_from_session_not_from_model_arguments(server):
    url, _ = server
    # The "model" tries to smuggle another customer's id into the call.
    client = ScriptedClient(call("get_balance", customer_id=B), Message("assistant", ["listo"]))
    response, recorder, requests, _ = converse(url, client, "¿Cuál es mi saldo?", approve=False)

    assert requests == []  # reads need no approval
    assert recorder.names() == ["get_balance"]
    numbers = [c["numero"] for c in tool_output(response)["cuentas"]]
    assert numbers == [f"000{A}"]  # customer A's data: the header won, not the argument


def test_denied_write_never_reaches_the_server(server):
    url, engine = server
    client = ScriptedClient(
        call("block_card", card_id=CARD_A), Message("assistant", ["No se hizo."])
    )
    response, recorder, requests, _ = converse(url, client, "Bloquea mi tarjeta", approve=False)

    assert requests == ["block_card"]
    assert recorder.names() == []
    assert response.text == "No se hizo."
    with engine.connect() as conn:
        assert conn.execute(select(cards.c.estado).where(cards.c.id == CARD_A)).scalar() == "activa"
        tools = conn.execute(select(audit_log.c.tool)).scalars().all()
    assert "block_card" not in tools


def test_approved_write_runs_once_with_conversation_id(server):
    url, engine = server
    client = ScriptedClient(call("block_card", card_id=CARD_A), Message("assistant", ["Listo."]))
    _, recorder, requests, session = converse(url, client, "Bloquea mi tarjeta", approve=True)

    assert requests == ["block_card"]
    assert recorder.calls == [("block_card", {"card_id": CARD_A})]
    with engine.connect() as conn:
        assert conn.execute(select(cards.c.estado).where(cards.c.id == CARD_A)).scalar() == (
            "bloqueada"
        )
        row = conn.execute(select(audit_log).where(audit_log.c.tool == "block_card")).one()
    assert (row.cliente_id, row.resultado, row.conversation_id) == (A, "ok", session.session_id)
