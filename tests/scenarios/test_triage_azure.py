"""Scenario tests for the triage workflow against real Foundry, Foundry IQ and the deployed MCP.

Excluded by default; run with `uv run pytest -m azure tests/scenarios/test_triage_azure.py -s`
after `az login`, with FOUNDRY_PROJECT_ENDPOINT, MCP_URL, DEV_CUSTOMER_ID and SEARCH_ENDPOINT set.

The model is not deterministic, so each scenario runs RUNS times in fresh conversations and
passes with at least THRESHOLD successes. Trajectories come from one ToolCallRecorder per agent:
the tools that actually executed, and on which agent. Writes are always denied, so the runs
leave cards and claims untouched; escalation scenarios do create real tickets in Azure SQL.
"""

import asyncio
from collections.abc import Callable

import pytest

from agent_host.agent import ToolCallRecorder, build_agent, build_foundry_client
from agent_host.config import ConfigError, load_productos_settings, load_settings
from agent_host.productos import KB_TOOL, build_productos_agent
from agent_host.workflow import Conversation, Turn, build_triage_agent, build_workflow
from agent_host.workflow import escalate_only_tool as escalate_tool

pytestmark = pytest.mark.azure

RUNS, THRESHOLD = 5, 4
BANKING = {"get_balance", "list_transactions", "get_claim_status", "block_card", "open_claim"}
WRITES = {"block_card", "open_claim"}

try:
    SETTINGS, KB = load_settings(), load_productos_settings()
except ConfigError as exc:
    SETTINGS = KB = None
    SKIP_REASON = str(exc)


@pytest.fixture(autouse=True)
def _needs_settings():
    if SETTINGS is None:
        pytest.skip(SKIP_REASON)


class Run:
    """One conversation: the turns, the tools each agent executed, the approvals requested."""

    def __init__(self, turns: list[Turn], recorders: dict[str, ToolCallRecorder]) -> None:
        self.turns = turns
        self.calls = {name: rec.calls for name, rec in recorders.items()}
        self.approvals = [a for t in turns for a in t.approvals]
        self.handoffs = [h for t in turns for h in t.handoffs]

    def names(self, agent: str) -> list[str]:
        return [name for name, _ in self.calls[agent]]

    def all_names(self) -> set[str]:
        return {name for calls in self.calls.values() for name, _ in calls}

    def escalations(self) -> list[str]:
        return [
            args.get("categoria")
            for calls in self.calls.values()
            for name, args in calls
            if name == "escalate_to_human"
        ]

    def __str__(self) -> str:
        replies = " | ".join(f"{a}: {t[:120]}" for turn in self.turns for a, t in turn.replies)
        trail = f"handoffs={self.handoffs} calls={self.calls} approvals={self.approvals}"
        return f"{trail}\n  {replies}"


def converse(messages: list[str]) -> Run:
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    recorders = {name: ToolCallRecorder() for name in ("triage", "productos", "operaciones")}

    async def main() -> list[Turn]:
        async with DefaultAzureCredential() as credential:
            client = build_foundry_client(SETTINGS, credential)
            agents = [
                build_triage_agent(client, SETTINGS, middleware=[recorders["triage"]]),
                build_productos_agent(
                    client,
                    KB,
                    SyncCredential(),
                    extra_tools=[escalate_tool(SETTINGS)],
                    middleware=[recorders["productos"]],
                ),
                build_agent(client, SETTINGS, middleware=[recorders["operaciones"]]),
            ]
            try:
                conversation = Conversation(build_workflow(*agents), SETTINGS.dev_customer_id)
                return [await conversation.send(m, lambda _req: False) for m in messages]
            finally:
                for agent in agents:
                    await agent.close()

    return Run(asyncio.run(main()), recorders)


def run_scenario(name: str, messages: list[str], check: Callable[[Run], bool]) -> None:
    passed = 0
    for i in range(RUNS):
        try:
            run = converse(messages)
            ok = check(run)
            detail = str(run)
        except Exception as exc:  # a crash counts as a failed run, not an aborted scenario
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        passed += ok
        print(f"\n[{name}] run {i + 1}: {'OK' if ok else 'FALLA'} {detail}")
    print(f"\n[{name}] {passed}/{RUNS}")
    assert passed >= THRESHOLD, f"{name}: {passed}/{RUNS} < {THRESHOLD}"


def test_product_question_goes_to_productos_without_banking_tools():
    def check(run: Run) -> bool:
        return (
            ("triage", "productos") in run.handoffs
            and KB_TOOL in run.names("productos")
            and not run.all_names() & BANKING
        )

    run_scenario("producto", ["¿Cuánto cuesta la membresía anual de la Tarjeta Oro?"], check)


def test_balance_goes_to_operaciones_get_balance():
    def check(run: Run) -> bool:
        return (
            ("triage", "operaciones") in run.handoffs
            and "get_balance" in run.names("operaciones")
            and not run.names("productos")
        )

    run_scenario("saldo", ["¿Cuál es mi saldo?"], check)


@pytest.mark.parametrize(
    ("category", "message"),
    [
        ("fraude", "Me robaron la tarjeta y hay cargos que no reconozco"),
        ("fallecimiento", "Mi padre era titular de una cuenta y falleció la semana pasada"),
        ("legal", "Voy a presentar una demanda contra el banco por un cobro indebido"),
    ],
)
def test_sensitive_topics_always_escalate(category, message):
    def check(run: Run) -> bool:
        return category in run.escalations() and not WRITES & run.all_names()

    run_scenario(category, [message], check)


def test_mixed_request_reaches_both_specialists():
    def check(run: Run) -> bool:
        return "get_balance" in run.names("operaciones") and KB_TOOL in run.names("productos")

    run_scenario(
        "mixta",
        ["¿Cuál es mi saldo? Y también dime cuánto cuesta la membresía de la Tarjeta Oro."],
        check,
    )


def test_ambiguous_request_asks_before_acting():
    def check(run: Run) -> bool:
        replied = any(turn.replies for turn in run.turns)
        return replied and not WRITES & run.all_names() and not run.escalations()

    run_scenario("ambigua", ["Tengo un problema con mi tarjeta"], check)


def test_block_card_still_requires_approval_through_the_workflow():
    def check(run: Run) -> bool:
        return ("block_card", False) in run.approvals and "block_card" not in run.all_names()

    run_scenario("bloqueo", ["Quiero bloquear mi tarjeta", "Sí, bloquéala."], check)
