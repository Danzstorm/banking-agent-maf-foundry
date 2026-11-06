import pytest

from mcp_server.auth import AuthError, resolve_customer
from tests.conftest import A


def test_dev_mode_resolves_existing_customer(engine, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "dev")
    assert resolve_customer({"x-dev-customer-id": str(A)}, engine) == A


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"x-dev-customer-id": "abc"},
        {"x-dev-customer-id": "999"},
        {"x-dev-customer-id": "-1"},
        {"x-dev-customer-id": "²"},
    ],
)
def test_dev_mode_rejects_missing_or_invalid_identity(engine, monkeypatch, headers):
    monkeypatch.setenv("AUTH_MODE", "dev")
    with pytest.raises(AuthError):
        resolve_customer(headers, engine)


@pytest.mark.parametrize("mode", [None, "", "prod", "entra", "DEV"])
def test_dev_header_alone_never_authenticates_outside_dev_mode(engine, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTH_MODE", mode)
    with pytest.raises(AuthError):
        resolve_customer({"x-dev-customer-id": str(A)}, engine)
