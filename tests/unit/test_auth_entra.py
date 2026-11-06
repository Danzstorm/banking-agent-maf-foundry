"""AUTH_MODE=entra: JWT validation and oid -> customer mapping, with locally generated RSA keys.

Red team at the identity layer: a customer's token can only ever resolve to that customer, and
nothing the caller controls besides a valid token (dev header, forged claims) changes that.
"""

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server import auth
from mcp_server.auth import AuthError, require_bearer, resolve_customer
from tests.conftest import A, B

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://{TENANT}.ciamlogin.com/{TENANT}/v2.0"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def entra_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "entra")
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("ENTRA_ISSUER", ISSUER)
    monkeypatch.setenv("ENTRA_JWKS_URI", "https://keys.invalid/keys")
    # The tenant's JWKS publishes only KEY's public half.
    monkeypatch.setattr(auth, "signing_key", lambda token, uri: KEY.public_key())


def token(key=KEY, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "tid": TENANT,
        "oid": f"oid-{A}",
        "scp": "tools.access",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


def bearer(value: str) -> dict[str, str]:
    return {"authorization": f"Bearer {value}"}


def test_valid_token_resolves_its_own_customer(engine):
    assert resolve_customer(bearer(token()), engine) == A
    assert resolve_customer(bearer(token(oid=f"oid-{B}")), engine) == B


def test_scope_may_come_with_others(engine):
    assert resolve_customer(bearer(token(scp="openid tools.access")), engine) == A


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://someone-else"},
        {"aud": "00000003-0000-0000-c000-000000000000"},  # an ms-graph token
        {"iss": "https://login.microsoftonline.com/evil/v2.0"},
        {"tid": "33333333-3333-3333-3333-333333333333"},
        {"exp": int(time.time()) - 3600},
        {"nbf": int(time.time()) + 3600},
        {"scp": "access_as_user"},  # the agent API's token, replayed at MCP
        {"scp": None},
        {"oid": None},
        {"oid": "oid-unknown"},  # valid token, but not a bank customer
        {"exp": None},
    ],
)
def test_rejects_bad_claims(engine, overrides):
    with pytest.raises(AuthError, match="^No autorizado.$"):
        resolve_customer(bearer(token(**overrides)), engine)


def test_rejects_bad_signature(engine):
    with pytest.raises(AuthError):
        resolve_customer(bearer(token(key=OTHER_KEY)), engine)


def test_rejects_tampered_payload(engine):
    header, _, signature = token().split(".")
    forged = token(key=OTHER_KEY, oid=f"oid-{B}").split(".")[1]
    with pytest.raises(AuthError):
        resolve_customer(bearer(f"{header}.{forged}.{signature}"), engine)


def test_rejects_symmetric_and_unsigned_tokens(engine):
    hs = jwt.encode({"oid": f"oid-{A}"}, "x" * 32, algorithm="HS256")
    unsigned = jwt.encode({"oid": f"oid-{A}"}, None, algorithm="none")
    for value in (hs, unsigned):
        with pytest.raises(AuthError):
            resolve_customer(bearer(value), engine)


@pytest.mark.parametrize(
    "headers",
    [None, {}, {"authorization": ""}, {"authorization": "Basic abc"}, bearer("garbage")],
)
def test_rejects_missing_or_malformed_header(engine, headers):
    with pytest.raises(AuthError):
        resolve_customer(headers, engine)


def test_dev_header_is_ignored(engine):
    with pytest.raises(AuthError):
        resolve_customer({"x-dev-customer-id": str(A)}, engine)
    # With a valid token for A, a dev header naming B changes nothing.
    assert resolve_customer(bearer(token()) | {"x-dev-customer-id": str(B)}, engine) == A


def test_fails_closed_when_misconfigured(engine, monkeypatch):
    monkeypatch.delenv("ENTRA_AUDIENCE")
    with pytest.raises(AuthError):
        resolve_customer(bearer(token()), engine)


def _status(headers: dict[str, str], path="/mcp") -> int:
    called = []

    async def app(scope, receive, send):
        called.append(scope["path"])

    sent = []

    async def send(message):
        sent.append(message)

    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "path": path, "headers": raw}
    asyncio.run(require_bearer(app)(scope, None, send))
    return sent[0]["status"] if sent else (200 if called else 0)


def test_http_gate_returns_401_without_valid_token():
    assert _status({}) == 401
    assert _status(bearer("garbage")) == 401
    assert _status(bearer(token(aud="other"))) == 401
    assert _status(bearer(token())) == 200
    assert _status({}, path="/healthz") == 200
