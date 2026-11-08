"""Deterministic part of the evaluation runner (no Azure, no LLM)."""

from evals.run_evals import check, consistency, openai_endpoint, render, to_row


def row(response="", executed=(), expect=None, group="", **extra):
    return {
        "id": "x",
        "group": group,
        "response": response,
        "executed": list(executed),
        "sources": [],
        "escalations": [],
        "approvals": [],
        "expectations": expect or {},
    } | extra


def test_check_reports_each_failed_expectation():
    expect = {
        "tools": ["get_balance"],
        "forbid": ["block_card"],
        "contains": [r"\b4\b"],
        "not_contains": ["4763037182"],
        "sources": ["Tarjeta de Crédito Oro"],
        "escalation": "fraude",
        "approval": ["block_card"],
    }
    failures = check(row("cuenta 4763037182", ["block_card"], expect))
    assert len(failures) == 7, failures
    ok = row(
        "Incluye 4 accesos.",
        ["get_balance"],
        expect,
        sources=["Tarjeta de Crédito Oro"],
        escalations=["fraude"],
        approvals=[["block_card", False]],
    )
    assert check(ok) == []


def test_consistency_compares_figures_and_sources():
    same = [row("Incluye 4 accesos.", group="g", sources=["A"]) for _ in range(3)]
    assert consistency(same) == {"g": (3, 3, True)}
    other = [*same[:2], row("Incluye 6 accesos.", group="g", sources=["A"])]
    assert consistency(other)["g"][2] is False


def test_to_row_drops_handoffs_from_judged_tool_calls_and_renders_product_json():
    trace = {
        "replies": [("productos", '{"answer": "S/ 299 al año.", "sources": ["Oro"]}')],
        "handoffs": [("triage", "productos")],
        "approvals": [],
        "calls": [
            {"agent": "triage", "name": "handoff_to_productos", "arguments": {}, "result": ""},
            {
                "agent": "productos",
                "name": "knowledge_base_retrieve",
                "arguments": {},
                "result": "",
            },
        ],
        "definitions": {"handoff_to_productos": {}, "knowledge_base_retrieve": {"name": "k"}},
        "error": None,
    }
    r = to_row({"id": "p", "category": "productos", "turns": ["¿Cuánto?"]}, trace)
    assert [c["name"] for c in r["tool_calls"]] == ["knowledge_base_retrieve"]
    assert r["tool_definitions"] == [{"name": "k"}]
    assert r["executed"] == ["handoff_to_productos", "knowledge_base_retrieve"]
    assert r["sources"] == ["Oro"] and r["response"].startswith("S/ 299")
    assert render("texto plano") == ("texto plano", [])


def test_openai_endpoint_from_project_endpoint():
    url = "https://aif-x.services.ai.azure.com/api/projects/p"
    assert openai_endpoint(url) == "https://aif-x.openai.azure.com"
