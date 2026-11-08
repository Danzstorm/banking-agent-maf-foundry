"""A2A interoperability (Phase 7): the Productos A2A server over real HTTP, spoken to with plain
httpx and hand-written JSON-RPC. No MAF client, no a2a-sdk client: if a non-MAF agent can do
this, it can talk to Productos. The model is scripted; no Azure.
"""

import asyncio
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn
from agent_framework import Agent, Message
from agent_framework.a2a import A2AAgent

from agent_host.productos import BearerAuth
from agent_host.productos_a2a import create_app
from mcp_server.auth import AuthError
from tests.conftest import _free_port
from tests.contract.test_agent_loop import ScriptedClient

pytestmark = pytest.mark.contract

TOKEN = "service-token"
ANSWER = {"answer": "La comisión de mantenimiento es S/ 10 al mes.", "sources": ["Tarifario"]}


def accept_test_token(headers):
    if headers.get("authorization") != f"Bearer {TOKEN}":
        raise AuthError()


@pytest.fixture(scope="module")
def a2a_url():
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    # Same response shape as the real Productos agent (ProductAnswer JSON, ADR-018).
    client = ScriptedClient(*[Message("assistant", [json.dumps(ANSWER)]) for _ in range(5)])
    agent = Agent(client=client, name="productos")
    app = create_app(agent, url=url, validate=accept_test_token)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("A2A server did not start")
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def send_message(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {"messageId": str(uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
        },
    }


HEADERS = {"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"}


def test_card_is_public_and_declares_endpoint_and_bearer_scheme(a2a_url):
    response = httpx.get(f"{a2a_url}.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "productos"
    interface = card["supportedInterfaces"][0]
    assert interface == {"url": a2a_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["securitySchemes"]["entra"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert card["securityRequirements"] == [{"schemes": {"entra": {}}}]
    assert card["skills"][0]["id"] == "productos"


def test_send_message_returns_a_completed_task_with_the_answer(a2a_url):
    response = httpx.post(
        a2a_url, json=send_message("¿Comisión de mantenimiento?"), headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    agent_texts = [
        part["text"]
        for message in task["history"]
        if message["role"] == "ROLE_AGENT"
        for part in message["parts"]
    ]
    assert json.loads(agent_texts[-1]) == ANSWER


def test_send_message_without_token_is_401(a2a_url):
    response = httpx.post(
        a2a_url, json=send_message("hola"), headers={"A2A-Version": "1.0"}, timeout=10
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_pre_1_0_clients_get_a_version_error(a2a_url):
    """No `A2A-Version` header means 0.3; this server speaks 1.0 only."""
    response = httpx.post(
        a2a_url, json=send_message("hola"), headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.json()["error"]["code"] == -32009


def test_the_agent_host_a2a_client_gets_the_answer(a2a_url):
    """The other side of the wire: the client the agent host uses (A2AAgent + bearer auth)."""

    async def ask() -> str:
        auth = BearerAuth(lambda _kwargs: {"Authorization": f"Bearer {TOKEN}"})
        async with httpx.AsyncClient(auth=auth, timeout=10) as http:
            remote = A2AAgent(name="productos-a2a", url=a2a_url, http_client=http)
            return (await remote.run("¿Comisión de mantenimiento?")).text

    assert json.loads(asyncio.run(ask())) == ANSWER
