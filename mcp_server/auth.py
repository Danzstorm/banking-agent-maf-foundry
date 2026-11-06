"""The single place where a request becomes a customer id.

Phase 1: AUTH_MODE=dev trusts the `X-Dev-Customer-Id` header (local development only).
Phase 5 replaces the body of `resolve_customer` with Entra JWT validation (`oid` -> customer).
Any other mode, or a missing/unknown identity, is refused: the server fails closed.
"""

import os
from collections.abc import Mapping

from sqlalchemy import Engine, select

from mcp_server.db import customers

DEV_CUSTOMER_HEADER = "x-dev-customer-id"
CONVERSATION_HEADER = "x-conversation-id"


class AuthError(Exception):
    """The request carries no valid customer identity."""


def resolve_customer(headers: Mapping[str, str] | None, engine: Engine) -> int:
    if os.environ.get("AUTH_MODE") != "dev":
        raise AuthError("No autorizado.")
    raw = (headers or {}).get(DEV_CUSTOMER_HEADER, "").strip()
    if not (raw.isascii() and raw.isdigit()):
        raise AuthError("No autorizado.")
    with engine.connect() as conn:
        customer_id = conn.execute(
            select(customers.c.id).where(customers.c.id == int(raw))
        ).scalar_one_or_none()
    if customer_id is None:
        raise AuthError("No autorizado.")
    return customer_id
