import pytest
from agent_framework import Content

from agent_host.agent import (
    CONVERSATION_KEY,
    CUSTOMER_KEY,
    READ_TOOLS,
    WRITE_TOOLS,
    build_mcp_tool,
    mcp_headers,
)
from agent_host.cli import describe_request, format_reply, parse_answer
from agent_host.config import ConfigError, Settings, load_settings

ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://aif.services.ai.azure.com/api/projects/p",
    "MCP_URL": "https://mcp.example/mcp",
    "DEV_CUSTOMER_ID": "1",
}


def test_settings_from_env_with_default_model():
    s = load_settings(ENV)
    assert (s.model_deployment, s.dev_customer_id, s.mcp_url) == ("gpt-5-mini", 1, ENV["MCP_URL"])


@pytest.mark.parametrize(
    "override",
    [
        {"DEV_CUSTOMER_ID": ""},
        {"DEV_CUSTOMER_ID": "abc"},
        {"DEV_CUSTOMER_ID": "0"},
        {"MCP_URL": "http://mcp.example/mcp"},  # plain http only for localhost
        {"FOUNDRY_PROJECT_ENDPOINT": "ftp://x"},
    ],
)
def test_invalid_settings_are_rejected(override):
    with pytest.raises(ConfigError):
        load_settings(ENV | override)


def test_http_allowed_for_local_mcp():
    assert load_settings(ENV | {"MCP_URL": "http://127.0.0.1:8000/mcp"}).mcp_url.startswith("http:")


def test_headers_come_only_from_host_context():
    # Anything else in the mapping (e.g. a model-style customer_id) is ignored.
    kwargs = {CUSTOMER_KEY: 1, CONVERSATION_KEY: "conv-1", "customer_id": 2, "session": object()}
    assert mcp_headers(kwargs) == {"X-Dev-Customer-Id": "1", "X-Conversation-Id": "conv-1"}


def test_headers_without_host_context_fail_closed():
    with pytest.raises(KeyError):
        mcp_headers({"customer_id": 2})


def test_writes_require_approval_by_default_but_escalation_does_not():
    tool = build_mcp_tool(Settings("https://f.example", "m", "https://mcp.example/mcp", 1))
    assert tool.approval_mode == {
        "default": "always_require",
        "always_require_approval": WRITE_TOOLS,
        "never_require_approval": [*READ_TOOLS, "escalate_to_human"],
    }


def test_approval_prompt_in_spanish():
    call = Content.from_function_call(call_id="c1", name="block_card", arguments='{"card_id": 7}')
    req = Content.from_function_approval_request(id="c1", function_call=call)
    assert describe_request(req) == 'Bloquear tarjeta (block_card) con {"card_id": 7}'


@pytest.mark.parametrize(
    ("answer", "approved"), [("s", True), (" Sí ", True), ("n", False), ("", False)]
)
def test_parse_answer(answer, approved):
    assert parse_answer(answer) is approved


def test_productos_json_reply_is_shown_with_sources():
    reply = '{"answer": "Cuesta S/ 10.", "sources": ["Tarifario"]}'
    assert format_reply("productos", reply) == "[Productos] Cuesta S/ 10.\nFuentes: Tarifario"
    assert format_reply("triage", "Hola") == "[Triage] Hola"


ENTRA_ENV = ENV | {
    "AUTH_MODE": "entra",
    "DEV_CUSTOMER_ID": "",
    "ENTRA_AUTHORITY": "https://tenant.ciamlogin.com/tid",
    "ENTRA_CLI_CLIENT_ID": "cli",
    "ENTRA_AGENT_API_CLIENT_ID": "agent",
    "ENTRA_MCP_API_CLIENT_ID": "mcp",
    "KEY_VAULT_URI": "https://kv.vault.azure.net/",
}


def test_entra_settings_derive_scopes_and_need_no_dev_customer():
    s = load_settings(ENTRA_ENV)
    assert s.dev_customer_id is None
    assert (s.entra.agent_scope, s.entra.mcp_scope) == (
        "api://agent/access_as_user",
        "api://mcp/tools.access",
    )
    assert s.entra.cert_name == "agent-obo"


@pytest.mark.parametrize(
    "override",
    [{"ENTRA_MCP_API_CLIENT_ID": ""}, {"KEY_VAULT_URI": "http://kv"}, {"AUTH_MODE": "x"}],
)
def test_invalid_entra_settings_are_rejected(override):
    with pytest.raises(ConfigError):
        load_settings(ENTRA_ENV | override)


def test_entra_identity_sends_bearer_and_never_the_dev_header():
    kwargs = {CUSTOMER_KEY: lambda: "tok", CONVERSATION_KEY: "conv-1", "customer_id": 2}
    assert mcp_headers(kwargs) == {"Authorization": "Bearer tok", "X-Conversation-Id": "conv-1"}


def test_failed_obo_raises_a_safe_error(monkeypatch):
    from agent_host import identity as mod

    class FakePublic:
        def __init__(self, *a, **k):
            pass

        def get_accounts(self):
            return [{"username": "u"}]

        def acquire_token_silent(self, scopes, account):
            return {"access_token": "user-token"}

    class FakeConfidential(FakePublic):
        def acquire_token_on_behalf_of(self, user_assertion, scopes):
            assert (user_assertion, scopes) == ("user-token", ["api://mcp/tools.access"])
            return {"error": "invalid_grant", "error_description": "secret details"}

    monkeypatch.setattr(mod.msal, "PublicClientApplication", FakePublic)
    monkeypatch.setattr(mod.msal, "ConfidentialClientApplication", FakeConfidential)
    ident = mod.EntraIdentity(load_settings(ENTRA_ENV).entra, lambda: "pem")
    with pytest.raises(mod.IdentityError) as exc:
        ident.mcp_token()
    assert "secret" not in str(exc.value)
