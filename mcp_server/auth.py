"""The single place where a request becomes a customer id.

AUTH_MODE=dev   trusts the `X-Dev-Customer-Id` header (local development only).
AUTH_MODE=entra validates the Entra External ID access token (`Authorization: Bearer`) and maps
                its `oid` to `clientes.entra_oid`. The dev header is ignored.
Any other mode, or a missing/invalid/unknown identity, is refused: the server fails closed with
one generic message, so a caller never learns which check failed.
"""

import os
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

import anyio
import jwt
from sqlalchemy import Engine, select

from mcp_server.db import customers

DEV_CUSTOMER_HEADER = "x-dev-customer-id"
CONVERSATION_HEADER = "x-conversation-id"
REQUIRED_SCOPE = "tools.access"
MAX_TOKEN_LENGTH = 8192
CLOCK_SKEW_SECONDS = 60
ENTRA_ENV = ("ENTRA_TENANT_ID", "ENTRA_ISSUER", "ENTRA_AUDIENCE", "ENTRA_JWKS_URI")


class AuthError(Exception):
    """The request carries no valid customer identity."""

    def __init__(self) -> None:
        super().__init__("No autorizado.")


@lru_cache(maxsize=4)
def _jwks_client(uri: str) -> jwt.PyJWKClient:
    # Caches the key set (5 min) and the keys; an unknown `kid` triggers one refetch (rotation).
    return jwt.PyJWKClient(uri, cache_keys=True, timeout=10)


def signing_key(token: str, jwks_uri: str) -> Any:
    """Public key that signed `token`, looked up by its `kid` in the tenant's JWKS."""
    return _jwks_client(jwks_uri).get_signing_key_from_jwt(token).key


def _bearer(headers: Mapping[str, str] | None) -> str:
    scheme, _, token = (headers or {}).get("authorization", "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_LENGTH:
        raise AuthError()
    return token


def validate_jwt(
    headers: Mapping[str, str] | None,
    *,
    tenant_id: str,
    issuer: str | Sequence[str],
    audience: str | Sequence[str],
    jwks_uri: str,
    scope: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Claims of a valid Entra access token for `audience`, or AuthError.

    Pass exactly one of `scope` (delegated token, `scp` claim) or `role` (app-only token,
    `roles` claim). Shared by the MCP server (tools.access), the agent host API
    (access_as_user) and the Productos A2A server (Productos.Invoke, ADR-015).
    """
    wanted = scope or role
    if not all((tenant_id, issuer, audience, jwks_uri, wanted)) or (scope and role):
        raise AuthError()  # misconfigured server: refuse everything
    claim = "scp" if scope else "roles"
    token = _bearer(headers)
    try:
        claims = jwt.decode(
            token,
            signing_key(token, jwks_uri),
            algorithms=["RS256"],  # never trust the token's own `alg` (no "none", no HS*)
            audience=audience,
            issuer=issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "nbf", "iss", "aud", "tid", "oid", claim]},
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise AuthError() from exc
    if claims["tid"] != tenant_id:
        raise AuthError()
    granted = str(claims["scp"]).split() if scope else claims["roles"]
    if not isinstance(granted, list) or wanted not in granted:
        raise AuthError()
    if not isinstance(claims["oid"], str) or not claims["oid"]:
        raise AuthError()
    return claims


def validate_token(headers: Mapping[str, str] | None) -> dict[str, Any]:
    """Claims of a valid MCP access token, or AuthError. No database access."""
    config = {name: os.environ.get(name, "").strip() for name in ENTRA_ENV}
    return validate_jwt(
        headers,
        tenant_id=config["ENTRA_TENANT_ID"],
        issuer=config["ENTRA_ISSUER"],
        audience=config["ENTRA_AUDIENCE"],
        jwks_uri=config["ENTRA_JWKS_URI"],
        scope=REQUIRED_SCOPE,
    )


def _lookup(engine: Engine, column, value) -> int:
    with engine.connect() as conn:
        customer_id = conn.execute(
            select(customers.c.id).where(column == value)
        ).scalar_one_or_none()
    if customer_id is None:
        raise AuthError()
    return customer_id


def resolve_customer(headers: Mapping[str, str] | None, engine: Engine) -> int:
    mode = os.environ.get("AUTH_MODE")
    if mode == "entra":
        return _lookup(engine, customers.c.entra_oid, validate_token(headers)["oid"])
    if mode == "dev":
        raw = (headers or {}).get(DEV_CUSTOMER_HEADER, "").strip()
        if not (raw.isascii() and raw.isdigit()):
            raise AuthError()
        return _lookup(engine, customers.c.id, int(raw))
    raise AuthError()


def require_bearer(app):
    """ASGI gate: in entra mode, /mcp without a valid token gets HTTP 401 before MCP runs.

    Defense in depth: tools still resolve the customer through `resolve_customer`.
    """

    async def gate(scope, receive, send):
        if (
            scope["type"] == "http"
            and os.environ.get("AUTH_MODE") == "entra"
            and scope["path"] != "/healthz"
        ):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            try:
                # JWKS refreshes do blocking HTTP: keep them off the event loop.
                await anyio.to_thread.run_sync(validate_token, headers)
            except AuthError:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"www-authenticate", b'Bearer error="invalid_token"'),
                            (b"content-type", b"text/plain; charset=utf-8"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b"No autorizado."})
                return
        await app(scope, receive, send)

    return gate
