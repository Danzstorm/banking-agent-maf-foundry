"""Red team over HTTP: the real server in AUTH_MODE=entra, tokens signed by a local JWKS. No Azure.

Customer A holds a valid token. Whatever A sends (other ids, a dev header naming B), A only ever
reaches A's data. Without a valid token, MCP answers HTTP 401 before any session exists.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import CARD_B, CLAIM_B, A, B, run_server
from tests.contract.test_mcp_contract import payload, run

pytestmark = pytest.mark.contract

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://{TENANT}.ciamlogin.com/{TENANT}/v2.0"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_uri():
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True) | {"kid": "k1"}
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/keys"
    httpd.shutdown()


@pytest.fixture(scope="module")
def entra_server(tmp_path_factory, jwks_uri):
    yield from run_server(
        tmp_path_factory,
        AUTH_MODE="entra",
        ENTRA_TENANT_ID=TENANT,
        ENTRA_AUDIENCE=AUDIENCE,
        ENTRA_ISSUER=ISSUER,
        ENTRA_JWKS_URI=jwks_uri,
    )


def headers_for(customer: int, **extra) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "tid": TENANT,
        "oid": f"oid-{customer}",
        "scp": "tools.access",
        "nbf": now,
        "exp": now + 600,
    }
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})
    return {"Authorization": f"Bearer {token}", "X-Conversation-Id": "red-team"} | extra


def test_without_token_http_401(entra_server):
    url, _ = entra_server
    for headers in ({}, {"Authorization": "Bearer garbage"}, {"X-Dev-Customer-Id": str(A)}):
        response = httpx2.post(url, headers=headers, json={}, timeout=5)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_each_token_sees_only_its_own_data(entra_server):
    url, _ = entra_server

    async def calls(client):
        return await client.call_tool("get_balance", {})

    assert [c["numero"] for c in payload(run(url, headers_for(A), calls))["cuentas"]] == ["0001"]
    assert [c["numero"] for c in payload(run(url, headers_for(B), calls))["cuentas"]] == ["0002"]


def test_token_of_a_cannot_touch_b(entra_server):
    url, engine = entra_server
    # The dev header naming B is ignored in entra mode.
    headers = headers_for(A, **{"X-Dev-Customer-Id": str(B)})

    async def calls(client):
        return [
            await client.call_tool("get_balance", {}),
            await client.call_tool("get_claim_status", {"claim_id": CLAIM_B}),
            await client.call_tool("block_card", {"card_id": CARD_B}),
        ]

    balance, claim, block = run(url, headers, calls)
    assert [c["numero"] for c in payload(balance)["cuentas"]] == ["0001"]
    assert claim.is_error
    assert block.is_error
    with engine.connect() as conn:
        estado = conn.exec_driver_sql("SELECT estado FROM tarjetas WHERE id = ?", (CARD_B,))
        assert estado.scalar_one() == "activa"
