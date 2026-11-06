import time
from types import SimpleNamespace

import pytest

from agent_host.cli import format_answer
from agent_host.config import ConfigError, load_productos_settings
from agent_host.productos import (
    KB_TOOL,
    SEARCH_SCOPE,
    ProductAnswer,
    SearchTokenHeaders,
    build_kb_tool,
)

ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://aif.services.ai.azure.com/api/projects/p",
    "SEARCH_ENDPOINT": "https://srch.search.windows.net/",
}


def test_productos_settings_defaults_and_mcp_url():
    s = load_productos_settings(ENV)
    assert (s.model_deployment, s.knowledge_base) == ("gpt-5-mini", "kb-productos")
    assert s.kb_mcp_url == (
        "https://srch.search.windows.net/knowledgebases/kb-productos/mcp"
        "?api-version=2026-08-01-preview"
    )


def test_productos_settings_do_not_need_operaciones_variables():
    assert "MCP_URL" not in ENV and "DEV_CUSTOMER_ID" not in ENV
    load_productos_settings(ENV)


@pytest.mark.parametrize(
    "override",
    [
        {"SEARCH_ENDPOINT": ""},
        {"SEARCH_ENDPOINT": "http://srch.search.windows.net"},
        {"KNOWLEDGE_BASE": "kb/../indexes"},
    ],
)
def test_invalid_productos_settings_are_rejected(override):
    with pytest.raises(ConfigError):
        load_productos_settings(ENV | override)


class FakeCredential:
    def __init__(self, lifetimes: list[float]) -> None:
        self.lifetimes = lifetimes
        self.calls: list[str] = []

    def get_token(self, scope: str):
        self.calls.append(scope)
        n = len(self.calls)
        return SimpleNamespace(token=f"t{n}", expires_on=time.time() + self.lifetimes[n - 1])


def test_search_headers_use_search_scope_and_cache_the_token():
    cred = FakeCredential([3600, 3600])
    headers = SearchTokenHeaders(cred)
    assert headers({}) == {"Authorization": "Bearer t1"}
    assert headers({"anything": 1}) == {"Authorization": "Bearer t1"}
    assert cred.calls == [SEARCH_SCOPE]


def test_search_headers_refresh_a_token_close_to_expiry():
    cred = FakeCredential([60, 3600])  # first token inside the 5-minute margin
    headers = SearchTokenHeaders(cred)
    headers({})
    assert headers({}) == {"Authorization": "Bearer t2"}


def test_kb_tool_is_read_only_and_limited_to_retrieve():
    tool = build_kb_tool(load_productos_settings(ENV), FakeCredential([3600]))
    assert tool.approval_mode == "never_require"
    assert list(tool.allowed_tools) == [KB_TOOL]


def test_answer_shows_sources_only_when_present():
    assert format_answer(ProductAnswer(answer="No tengo esa información.")) == (
        "No tengo esa información."
    )
    assert format_answer(ProductAnswer(answer="S/ 299", sources=["A", "B"])) == (
        "S/ 299\nFuentes: A; B"
    )
