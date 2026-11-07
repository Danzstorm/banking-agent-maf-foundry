"""Agent host HTTP service: auth, input limits, rate limit, durable sessions and ownership.

Plain scripted agents (no tools) keep this in-process and offline; the chaos test in
tests/contract/test_chaos.py covers approvals, MCP and killed replicas.
"""

import asyncio
import base64
import re
import time

import jwt
import pytest
from agent_framework import Agent, Message, WorkflowCheckpoint
from agent_framework.exceptions import WorkflowCheckpointException
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from starlette.testclient import TestClient

from agent_host.config import EntraSettings, Settings
from agent_host.service import Host, TokenBucket, create_app, dev_authenticator, entra_authenticator
from agent_host.sessions import SqlCheckpointStorage
from mcp_server import auth
from mcp_server.db import sessions
from tests.conftest import A, B
from tests.contract.test_agent_loop import ScriptedClient


class RecordingClient(ScriptedClient):
    """Scripted model that also records what it was sent."""

    def __init__(self, *script):
        super().__init__(*script)
        self.seen: list[list[str]] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.seen.append([m.text for m in messages])
        return super()._inner_get_response(
            messages=messages, stream=stream, options=options, **kwargs
        )


def participants(triage_client):
    def build():
        return [
            Agent(
                client=client,
                name=name,
                description=name,
                require_per_service_call_history_persistence=True,
            )
            for name, client in (
                ("triage", triage_client),
                ("productos", ScriptedClient()),
                ("operaciones", ScriptedClient()),
            )
        ]

    return build


def dev_host(engine, customer, triage_client, **overrides) -> Host:
    settings = Settings("https://unused.example", "scripted", "http://127.0.0.1:1/mcp", customer)
    return Host(
        engine=engine,
        authenticate=dev_authenticator(settings),
        build_participants=participants(triage_client),
        **overrides,
    )


def chat(host: Host, **body):
    with TestClient(create_app(host)) as client:
        return client.post("/chat", json=body)


def test_probes_answer_without_dependencies(engine):
    with TestClient(create_app(dev_host(engine, A, ScriptedClient()))) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}


def test_conversation_survives_a_restart_and_keeps_its_history(engine):
    first = chat(dev_host(engine, A, ScriptedClient(Message("assistant", ["Hola."]))), message="Hi")
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]
    assert first.json()["text"] == "Hola."

    # A brand-new host (another replica, or the same one after a restart) continues it.
    model = RecordingClient(Message("assistant", ["Sigo aquí."]))
    second = chat(dev_host(engine, A, model), conversation_id=conversation_id, message="¿Sigues?")
    assert second.status_code == 200
    assert second.json()["text"] == "Sigo aquí."
    assert model.seen == [["¿Sigues?"]]  # same as in-process: earlier turns live in the session

    latest = asyncio.run(SqlCheckpointStorage(engine, conversation_id, A).get_latest())
    triage = latest.state["_executor_state"]["triage"]
    assert [m.text for m in triage["full_conversation"]] == [
        "Hi",
        "Hola.",
        "¿Sigues?",
        "Sigo aquí.",
    ]


def test_another_customer_cannot_resume_the_conversation(engine):
    first = chat(dev_host(engine, A, ScriptedClient(Message("assistant", ["Hola."]))), message="Hi")
    conversation_id = first.json()["conversation_id"]
    other = chat(
        dev_host(engine, B, ScriptedClient()), conversation_id=conversation_id, message="x"
    )
    assert other.status_code == 404
    unknown = chat(dev_host(engine, A, ScriptedClient()), conversation_id="nope", message="x")
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"message": "x" * 2001}, 413),
        ({"message": "   "}, 400),
        ({}, 400),
        ({"message": "hola", "approvals": [{"request_id": "r", "approved": True}]}, 400),
        ({"approvals": [{"request_id": "r", "approved": "yes"}]}, 400),
        ({"approvals": [{"request_id": "r", "approved": True}]}, 400),  # no conversation
    ],
)
def test_invalid_input_is_rejected_before_any_model_call(engine, body, status):
    model = RecordingClient()
    assert chat(dev_host(engine, A, model), **body).status_code == status
    assert model.seen == []


def test_per_user_rate_limit(engine):
    script = [Message("assistant", ["ok"]) for _ in range(2)]
    host = dev_host(engine, A, ScriptedClient(*script), limiter=TokenBucket(per_minute=1, burst=2))
    with TestClient(create_app(host)) as client:
        codes = [client.post("/chat", json={"message": "hola"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_token_bucket_refills():
    now = [0.0]
    bucket = TokenBucket(per_minute=60, burst=1, clock=lambda: now[0])
    assert bucket.allow("u") and not bucket.allow("u")
    assert bucket.allow("other")  # buckets are per user
    now[0] = 1.0
    assert bucket.allow("u")


TENANT = "11111111-1111-1111-1111-111111111111"
AGENT_API = "33333333-3333-3333-3333-333333333333"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def entra_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://{TENANT}.ciamlogin.com/{TENANT}/v2.0",
        "aud": AGENT_API,
        "tid": TENANT,
        "oid": f"oid-{A}",
        "scp": "access_as_user",
        "nbf": now,
        "exp": now + 3600,
    } | overrides
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def entra_host(engine, monkeypatch):
    monkeypatch.setattr(auth, "signing_key", lambda token, uri: KEY.public_key())
    entra = EntraSettings(
        authority=f"https://demo.ciamlogin.com/{TENANT}",
        cli_client_id="cli",
        agent_api_client_id=AGENT_API,
        mcp_api_client_id="mcp",
        key_vault_uri="https://kv.example",
        cert_name="agent-obo",
    )
    settings = Settings("https://unused.example", "scripted", "http://127.0.0.1:1/mcp", None, entra)
    return Host(
        engine=engine,
        authenticate=entra_authenticator(settings, engine, obo=None),
        build_participants=participants(ScriptedClient(Message("assistant", ["Hola."]))),
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {entra_token(aud='00000003-0000-0000-c000-000000000000')}"},
        {"Authorization": f"Bearer {entra_token(scp='tools.access')}"},
        {"Authorization": f"Bearer {entra_token(oid='unknown-oid')}"},
    ],
)
def test_entra_mode_rejects_anything_but_an_agent_api_token_of_a_customer(entra_host, headers):
    with TestClient(create_app(entra_host)) as client:
        response = client.post("/chat", json={"message": "hola"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_entra_mode_accepts_the_customer_token_and_never_stores_it(entra_host, engine):
    user_token = entra_token()
    with TestClient(create_app(entra_host)) as client:
        headers = {"Authorization": f"Bearer {user_token}"}
        response = client.post("/chat", json={"message": "hola"}, headers=headers)
    assert (response.status_code, response.json()["text"]) == (200, "Hola.")
    with engine.connect() as conn:
        row = conn.execute(select(sessions)).one()
    assert row.cliente_id == A
    # Checkpoints embed pickles as base64: look inside them too.
    blobs = [row.estado_json.encode()] + [
        base64.b64decode(b) for b in re.findall(r'"([A-Za-z0-9+/=]{40,})"', row.estado_json)
    ]
    assert not any(user_token.encode() in blob for blob in blobs)


def test_checkpoint_round_trip_is_scoped_to_its_owner(engine):
    async def main():
        mine = SqlCheckpointStorage(engine, "conv-1", A)
        checkpoint = WorkflowCheckpoint(workflow_name="w", graph_signature_hash="h")
        checkpoint.state["nota"] = "ñandú"
        await mine.save(checkpoint)
        await mine.save(checkpoint)  # second save updates the same row
        restored = await mine.load(checkpoint.checkpoint_id)
        assert restored.state == {"nota": "ñandú"}
        with pytest.raises(WorkflowCheckpointException):
            await mine.load("another-id")
        assert await SqlCheckpointStorage(engine, "conv-1", B).get_latest() is None

    asyncio.run(main())
