"""Phase 7: service-to-service auth of the Productos A2A server and the A2A breaker fallback."""

import asyncio
import socket
import time

import httpx
import jwt
import pytest
from agent_framework import Agent, Message
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from agent_host.productos import A2A_TOOL, BearerAuth, build_productos_proxy
from agent_host.productos_a2a import INVOKE_ROLE, create_app, entra_validator
from agent_host.resilience import BREAKER_FALLBACK, CircuitBreaker, ResilientTools
from mcp_server import auth
from mcp_server.auth import AuthError
from tests.contract.test_agent_loop import ScriptedClient, call

TENANT = "11111111-1111-1111-1111-111111111111"
APP = "44444444-4444-4444-4444-444444444444"  # productos-a2a app registration (client id)
V2_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def tenant_keys(monkeypatch):
    monkeypatch.setattr(auth, "signing_key", lambda token, uri: KEY.public_key())


def mi_token(**overrides) -> str:
    """App-only token as Entra issues it to the agent host's managed identity."""
    now = int(time.time())
    claims = {
        "iss": V2_ISSUER,
        "aud": APP,
        "tid": TENANT,
        "oid": "mi-agent-host",
        "roles": [INVOKE_ROLE],
        "idtyp": "app",
        "nbf": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


validate = entra_validator(TENANT, APP)


@pytest.mark.parametrize(
    "overrides",
    [{}, {"iss": f"https://sts.windows.net/{TENANT}/", "aud": f"api://{APP}"}],  # v2 and v1
)
def test_accepts_the_agent_host_managed_identity(overrides):
    assert validate(bearer(mi_token(**overrides)))["oid"] == "mi-agent-host"


@pytest.mark.parametrize(
    "overrides",
    [
        {"roles": None},  # any identity of the tenant can get a token; only role holders pass
        {"roles": ["Other.Role"]},
        {"roles": INVOKE_ROLE},  # a string is not the roles list
        {"roles": None, "scp": INVOKE_ROLE},  # a delegated (user) token is not enough
        {"aud": "https://management.azure.com"},
        {"iss": "https://login.microsoftonline.com/other/v2.0"},
        {"tid": "33333333-3333-3333-3333-333333333333"},
        {"iss": f"https://{TENANT}.ciamlogin.com/{TENANT}/v2.0"},  # the External ID tenant
        {"exp": int(time.time()) - 3600},
    ],
)
def test_rejects_other_tokens(overrides):
    with pytest.raises(AuthError):
        validate(bearer(mi_token(**overrides)))


def test_server_answers_401_without_a_valid_token_but_serves_the_card():
    app = create_app(
        Agent(client=ScriptedClient(), name="productos"), url="http://x/", validate=validate
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}
    with TestClient(app) as client:
        assert client.get("/.well-known/agent-card.json").status_code == 200
        assert client.get("/healthz").status_code == 200
        for headers in ({}, bearer("garbage"), bearer(mi_token(roles=None))):
            response = client.post("/", json=body, headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"].startswith("Bearer")


def test_bearer_auth_adds_the_cached_managed_identity_token():
    class Credential:
        calls = 0

        def get_token(self, scope):
            Credential.calls += 1
            assert scope == f"api://{APP}/.default"
            return type("T", (), {"token": "mi-token", "expires_on": time.time() + 3600})()

    from agent_host.productos import SearchTokenHeaders

    auth_ = BearerAuth(SearchTokenHeaders(Credential(), scope=f"api://{APP}/.default"))
    for _ in range(2):
        request = httpx.Request("POST", "https://productos-a2a.internal.example/")
        assert next(auth_.auth_flow(request)).headers["authorization"] == "Bearer mi-token"
    assert Credential.calls == 1


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_productos_down_opens_the_breaker_and_the_model_offers_a_human():
    """Productos stopped: the A2A tool fails, the breaker opens, the model gets the escalation
    fallback (which tells it to offer escalate_to_human) and later turns skip the network."""
    from agent_framework.a2a import A2AAgent

    breaker = CircuitBreaker(threshold=1, reset_after_s=60)
    remote = A2AAgent(name="productos-a2a", url=f"http://127.0.0.1:{_closed_port()}/")

    async def turn() -> object:
        client = ScriptedClient(call(A2A_TOOL, pregunta="¿Comisión?"), Message("assistant", ["ok"]))
        guard = [ResilientTools(breaker, guarded=A2A_TOOL, attempts=1)]  # Windows refuses slowly
        proxy = build_productos_proxy(client, remote, middleware=guard)
        response = await proxy.run("¿Cuál es la comisión?", options={"response_format": None})
        return next(
            c.result for m in response.messages for c in m.contents if c.type == "function_result"
        )

    assert asyncio.run(turn()) == BREAKER_FALLBACK
    assert breaker.state == "open"
    assert asyncio.run(turn()) == BREAKER_FALLBACK  # open: no call goes out
    assert "escalate_to_human" in BREAKER_FALLBACK
