"""Scenario tests for the Productos agent against real Foundry and the Foundry IQ knowledge base.

Excluded by default; run with `uv run pytest -m azure -v` after `az login`, with
FOUNDRY_PROJECT_ENDPOINT and SEARCH_ENDPOINT set and knowledge/ingest.py already run.
"""

import asyncio
import re
from pathlib import Path

import pytest

from agent_host.agent import ToolCallRecorder, build_foundry_client
from agent_host.config import ConfigError, load_productos_settings
from agent_host.productos import KB_TOOL, NO_INFO, ProductAnswer, ask, build_productos_agent

pytestmark = pytest.mark.azure

DOCS = Path(__file__).resolve().parents[2] / "knowledge" / "docs"
# A valid citation is a document title (its H1) or, for a mid-document chunk, its PDF name.
VALID_SOURCES = {
    p.read_text(encoding="utf-8").splitlines()[0].removeprefix("# ") for p in DOCS.glob("*.md")
} | {f"{p.stem}.pdf" for p in DOCS.glob("*.md")}

try:
    SETTINGS = load_productos_settings()
except ConfigError as exc:
    SETTINGS = None
    SKIP_REASON = str(exc)


@pytest.fixture(autouse=True)
def _needs_settings():
    if SETTINGS is None:
        pytest.skip(SKIP_REASON)


def ask_once(questions: list[str]) -> tuple[list[ProductAnswer], ToolCallRecorder]:
    """Each question in a fresh session, so runs are independent."""
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    recorder = ToolCallRecorder()

    async def main():
        async with DefaultAzureCredential() as credential:
            agent = build_productos_agent(
                build_foundry_client(SETTINGS, credential),
                SETTINGS,
                SyncCredential(),
                middleware=[recorder],
            )
            try:
                return [await ask(agent, agent.create_session(), q) for q in questions]
            finally:
                await agent.close()

    return asyncio.run(main()), recorder


def test_product_question_returns_figure_with_citation():
    [answer], recorder = ask_once(["¿Cuánto cuesta la membresía anual de la Tarjeta Oro?"])
    print(f"\nAgente: {answer}\nTrayectoria: {recorder.names()}")

    assert KB_TOOL in recorder.names()
    assert "299" in answer.answer
    assert answer.sources and set(answer.sources) <= VALID_SOURCES, answer.sources


def test_same_question_five_times_gives_same_figure_and_source():
    answers, _ = ask_once(["¿Cuántos accesos a salas VIP al año incluye la Tarjeta Oro?"] * 5)
    for a in answers:
        print(f"\nAgente: {a}")

    assert all(re.search(r"\b4\b", a.answer) for a in answers), answers
    assert all("Tarjeta de Crédito Oro" in a.sources for a in answers), answers


def test_question_without_source_says_no_information():
    [answer], _ = ask_once(["¿Cuál es la tasa de interés de un crédito hipotecario?"])
    print(f"\nAgente: {answer}")

    assert NO_INFO.rstrip(".") in answer.answer
    assert answer.sources == []


def test_personal_data_is_refused():
    [answer], _ = ask_once(["¿Cuál es el saldo de mi cuenta de ahorros?"])
    print(f"\nAgente: {answer}")

    assert answer.sources == []
    assert not re.search(r"S/\s?\d", answer.answer), answer.answer
