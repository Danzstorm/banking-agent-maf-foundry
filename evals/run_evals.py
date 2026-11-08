"""Agent evaluation (phase 8): drive the triage workflow over a dataset, score it with
azure-ai-evaluation and log the run to the Foundry project (Foundry > Evaluations).

The deployed MCP has internal ingress (phase 6), so this runner starts a local MCP server: same
code, AUTH_MODE=dev, a fresh SQLite database with the reproducible seed. The model (gpt-5-mini)
and the knowledge base (Foundry IQ) are the real Azure ones. Productos runs in process.

    export FOUNDRY_PROJECT_ENDPOINT=$(terraform -chdir=infra output -raw foundry_project_endpoint)
    export SEARCH_ENDPOINT=$(terraform -chdir=infra output -raw search_endpoint)
    uv run --group evals python -m evals.run_evals --name baseline
    uv run --group evals python -m evals.run_evals --dataset evals/adversarial_cases.jsonl

Each row gets deterministic checks (`expectations`: tools executed, tools forbidden, regex the
answer must or must not contain, citation, escalation) plus the LLM judges: tool_call_accuracy,
intent_resolution, task_adherence, relevance and, for rows with retrieved context, groundedness.
Rows sharing a `group` repeat one question (ADR-018 consistency: same figure, same source).
"""

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_framework import FunctionInvocationContext, FunctionMiddleware

ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("cases.jsonl")
RESULTS = Path(__file__).with_name("results")
CUSTOMER = 1  # seed customer used unless a case says otherwise
CONTEXT_LIMIT = 12_000  # characters of retrieved context sent to the groundedness judge
JUDGES = ("tool_call_accuracy", "intent_resolution", "task_adherence", "relevance", "groundedness")


class TraceRecorder(FunctionMiddleware):
    """Every tool call that executes on one agent: name, arguments, result and its definition."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.calls: list[dict[str, Any]] = []
        self.definitions: dict[str, dict[str, Any]] = {}

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        fn = context.function
        self.definitions[fn.name] = {
            "name": fn.name,
            "description": fn.description or "",
            "parameters": fn.parameters(),
        }
        call = {"agent": self.agent, "name": fn.name, "arguments": dict(context.arguments or {})}
        self.calls.append(call)
        await call_next()
        result = context.result
        if isinstance(result, list):
            result = "\n".join(getattr(c, "text", None) or str(c) for c in result)
        call["result"] = str(result)


@contextmanager
def local_mcp():
    """A seeded SQLite database and `python -m mcp_server` in dev mode; yields its URL."""
    from mcp_server.db import make_engine
    from mcp_server.seed import seed

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'evals.db'}"
        engine = make_engine(db_url)
        seed(engine)
        engine.dispose()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = os.environ | {"DATABASE_URL": db_url, "AUTH_MODE": "dev", "MCP_PORT": str(port)}
        proc = subprocess.Popen([sys.executable, "-m", "mcp_server"], cwd=ROOT, env=env)  # noqa: S603
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("El MCP local no arrancó.") from None
                    time.sleep(0.2)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def render(text: str) -> tuple[str, list[str]]:
    """Productos replies are ProductAnswer JSON: show answer plus sources, return the sources."""
    try:
        data = json.loads(text)
    except ValueError:
        return text, []
    if not isinstance(data, dict) or "answer" not in data:
        return text, []
    sources = [str(s) for s in data.get("sources") or []]
    return data["answer"] + (f"\nFuentes: {', '.join(sources)}" if sources else ""), sources


class Agents:
    """Builds the triage workflow (as tests/scenarios/test_triage_azure.py) for one conversation."""

    def __init__(self, project_endpoint: str, search_endpoint: str, mcp_url: str) -> None:
        from agent_host.config import Settings, load_productos_settings

        env = {"FOUNDRY_PROJECT_ENDPOINT": project_endpoint, "SEARCH_ENDPOINT": search_endpoint}
        self.kb = load_productos_settings(os.environ | env)
        self.mcp_url = mcp_url
        self._settings = Settings

    async def converse(self, turns: list[str], *, customer: int, approve: bool) -> dict[str, Any]:
        from azure.identity import DefaultAzureCredential as SyncCredential
        from azure.identity.aio import DefaultAzureCredential

        from agent_host.agent import build_agent, build_foundry_client
        from agent_host.productos import build_productos_agent
        from agent_host.workflow import Conversation, build_triage_agent, build_workflow
        from agent_host.workflow import escalate_only_tool as escalate_tool

        settings = self._settings(
            project_endpoint=self.kb.project_endpoint,
            model_deployment=self.kb.model_deployment,
            mcp_url=self.mcp_url,
            dev_customer_id=customer,
        )
        recorders = {name: TraceRecorder(name) for name in ("triage", "productos", "operaciones")}
        replies, handoffs, approvals, error = [], [], [], None
        async with DefaultAzureCredential() as credential:
            client = build_foundry_client(settings, credential)
            agents = [
                build_triage_agent(client, settings, middleware=[recorders["triage"]]),
                build_productos_agent(
                    client,
                    self.kb,
                    SyncCredential(),
                    extra_tools=[escalate_tool(settings)],
                    middleware=[recorders["productos"]],
                ),
                build_agent(client, settings, middleware=[recorders["operaciones"]]),
            ]
            try:
                conversation = Conversation(build_workflow(*agents), customer)
                for text in turns:
                    turn = await conversation.send(text, lambda _req: approve)
                    replies += turn.replies
                    handoffs += turn.handoffs
                    approvals += turn.approvals
                    if turn.approvals:  # follow-up turns only nudge a model that asked first
                        break
            except Exception as exc:  # a content filter block is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                for agent in agents:
                    await agent.close()
        return {
            "replies": replies,
            "handoffs": handoffs,
            "approvals": approvals,
            "calls": [c for r in recorders.values() for c in r.calls],
            "definitions": {k: v for r in recorders.values() for k, v in r.definitions.items()},
            "error": error,
        }


def to_row(case: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """One evaluation row: columns named as the azure-ai-evaluation evaluator parameters."""
    sources: list[str] = []
    texts = []
    for _agent, text in trace["replies"]:
        shown, found = render(text)
        texts.append(shown)
        sources += found
    response = "\n".join(texts) or (f"[sin respuesta] {trace['error']}" if trace["error"] else "")
    # Handoffs are routing, checked by the expectations; the judges see the specialist tools.
    calls = [c for c in trace["calls"] if not c["name"].startswith("handoff_to_")]
    tool_calls = [
        {
            "type": "tool_call",
            "tool_call_id": f"call_{i}",
            "name": c["name"],
            "arguments": c["arguments"],
        }
        for i, c in enumerate(calls)
    ]
    # Agent message format: the judges see which tool returned what before the final answer.
    messages = []
    for call, executed in zip(tool_calls, calls, strict=True):
        messages.append({"role": "assistant", "content": [call]})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["tool_call_id"],
                "content": [{"type": "tool_result", "tool_result": executed.get("result", "")}],
            }
        )
    messages.append({"role": "assistant", "content": [{"type": "text", "text": response}]})
    context = "\n\n".join(f"{c['name']}: {c.get('result', '')}" for c in calls)
    return {
        "id": case["id"],
        "category": case["category"],
        "group": case.get("group", ""),
        "query": "\n".join(case["turns"]),
        "query_messages": [
            {"role": "user", "content": [{"type": "text", "text": t}]} for t in case["turns"]
        ],
        "response": response,
        "messages": messages,
        "context": context[:CONTEXT_LIMIT],
        "tool_calls": tool_calls,
        "tool_definitions": [
            d for name, d in trace["definitions"].items() if not name.startswith("handoff_to_")
        ],
        "executed": [c["name"] for c in trace["calls"]],
        "escalations": [
            c["arguments"].get("categoria")
            for c in trace["calls"]
            if c["name"] == "escalate_to_human"
        ],
        "approvals": [list(a) for a in trace["approvals"]],
        "handoffs": [list(h) for h in trace["handoffs"]],
        "sources": sources,
        "error": trace["error"] or "",
        "expectations": case.get("expect", {}),
    }


def check(row: dict[str, Any]) -> list[str]:
    """Deterministic expectations of one row. Returns the failures (empty: the row passes)."""
    exp, executed, text = row["expectations"], set(row["executed"]), row["response"]
    failures = [f"no ejecutó {t}" for t in exp.get("tools", []) if t not in executed]
    failures += [f"ejecutó {t}" for t in exp.get("forbid", []) if t in executed]
    failures += [f"falta /{p}/" for p in exp.get("contains", []) if not re.search(p, text, re.I)]
    failures += [f"contiene /{p}/" for p in exp.get("not_contains", []) if re.search(p, text, re.I)]
    failures += [f"sin fuente {s}" for s in exp.get("sources", []) if s not in row["sources"]]
    if "escalation" in exp and exp["escalation"] not in row["escalations"]:
        failures.append(f"no escaló {exp['escalation']}")
    for tool in exp.get("approval", []):
        if tool not in {name for name, _ in row["approvals"]}:
            failures.append(f"no pidió aprobación de {tool}")
    return failures


class ExpectationsEvaluator:
    """Custom evaluator: the deterministic checks, as a pass/fail column in Foundry."""

    def __call__(
        self,
        *,
        response: str,
        executed: list,
        expectations: dict,
        sources: list,
        escalations: list,
        approvals: list,
    ) -> dict[str, Any]:
        failures = check(
            {
                "response": response,
                "executed": executed,
                "expectations": expectations,
                "sources": sources,
                "escalations": escalations,
                "approvals": approvals,
            }
        )
        return {"passed": float(not failures), "reason": "; ".join(failures) or "ok"}


def consistency(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int, bool]]:
    """Per group: (rows passing, rows, every row with the same figures and the same sources)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["group"]:
            groups[row["group"]].append(row)
    out = {}
    for name, members in groups.items():
        signatures = {
            (tuple(sorted(set(re.findall(r"\d+(?:[.,]\d+)?", r["response"])))), tuple(r["sources"]))
            for r in members
        }
        passed = sum(not check(r) for r in members)
        out[name] = (passed, len(members), len(signatures) == 1)
    return out


def openai_endpoint(project_endpoint: str) -> str:
    """https://X.services.ai.azure.com/api/projects/p -> https://X.openai.azure.com"""
    account = urlparse(project_endpoint).hostname.split(".")[0]
    return f"https://{account}.openai.azure.com"


def evaluate(rows_file: Path, out_dir: Path, name: str, project: str | None, model: str) -> dict:
    from azure.ai.evaluation import (
        GroundednessEvaluator,
        IntentResolutionEvaluator,
        RelevanceEvaluator,
        TaskAdherenceEvaluator,
        ToolCallAccuracyEvaluator,
    )
    from azure.ai.evaluation import evaluate as run_evaluate
    from azure.identity import DefaultAzureCredential

    # Rows judged in parallel per evaluator. Higher values hit the 50k TPM quota (429) with five
    # judges at once; the SDK retries, but slowly.
    os.environ.setdefault("PF_WORKER_COUNT", "2")
    credential = DefaultAzureCredential()
    model_config = {
        "azure_endpoint": openai_endpoint(os.environ["FOUNDRY_PROJECT_ENDPOINT"]),
        "azure_deployment": model,
    }
    judge = {"credential": credential, "is_reasoning_model": True}  # gpt-5-mini as the judge
    evaluators = {
        "tool_call_accuracy": ToolCallAccuracyEvaluator(model_config, **judge),
        "intent_resolution": IntentResolutionEvaluator(model_config, **judge),
        "task_adherence": TaskAdherenceEvaluator(model_config, **judge),
        "relevance": RelevanceEvaluator(model_config, **judge),
        "groundedness": GroundednessEvaluator(model_config, **judge),  # context: tool results
        "expectations": ExpectationsEvaluator(),
    }
    data = "${data.%s}"
    mapping = {k: data % k for k in ("query", "response")}
    agent_mapping = {
        "query": data % "query_messages",
        "response": data % "messages",
        "tool_definitions": data % "tool_definitions",
    }
    config = {
        # Without `response`, rows with no tool calls are "not applicable" instead of judged.
        "tool_call_accuracy": {
            "column_mapping": {k: data % k for k in ("query", "tool_calls", "tool_definitions")}
        },
        "intent_resolution": {"column_mapping": agent_mapping},
        "task_adherence": {"column_mapping": agent_mapping},
        "relevance": {"column_mapping": mapping},
        "groundedness": {"column_mapping": mapping | {"context": data % "context"}},
        "expectations": {
            "column_mapping": {
                k: data % k
                for k in (
                    "response",
                    "executed",
                    "expectations",
                    "sources",
                    "escalations",
                    "approvals",
                )
            }
        },
    }
    return run_evaluate(
        data=str(rows_file),
        evaluators=evaluators,
        evaluator_config=config,
        evaluation_name=name,
        azure_ai_project=project,
        output_path=str(out_dir / "evaluation.json"),
        credential=credential,
    )


def score(row: dict[str, Any], metric: str) -> str:
    value = row.get(f"outputs.{metric}.{metric}")
    if value is None or (isinstance(value, float) and value != value):  # missing or NaN
        return "n/a"
    return f"{value:g}" if isinstance(value, int | float) else str(value)


def summary(name: str, rows: list[dict[str, Any]], result: dict | None) -> str:
    """Markdown for the console, the results folder and the GitHub Job Summary."""
    lines = [f"## Evaluación `{name}`", ""]
    if result and result.get("studio_url"):
        lines += [f"Foundry: {result['studio_url']}", ""]
    passed = sum(not check(r) for r in rows)
    lines += [f"Expectativas deterministas: **{passed}/{len(rows)}**", ""]
    if result:
        metrics = result.get("metrics", {})
        lines += ["| Métrica | Valor |", "|---|---|"]
        for key, value in sorted(metrics.items()):
            if key.split(".")[0] in (*JUDGES, "expectations") and "token" not in key:
                lines.append(f"| `{key}` | {value:.2f} |")
        lines.append("")
    groups = consistency(rows)
    if groups:
        lines += ["| Consistencia | Correctas | Misma cifra y fuente |", "|---|---|---|"]
        for group, (ok, n, same) in groups.items():
            lines.append(f"| {group} | {ok}/{n} | {'sí' if same else 'no'} |")
        lines.append("")
    by_id = {r.get("inputs.id"): r for r in (result or {}).get("rows", [])}
    header = "| Caso | Categoría | Checks | " + " | ".join(JUDGES) + " |"
    lines += [header, "|" + "---|" * (3 + len(JUDGES))]
    for row in rows:
        failures = check(row)
        judged = by_id.get(row["id"], {})
        cells = " | ".join(score(judged, j) for j in JUDGES)
        mark = "ok" if not failures else "falla: " + "; ".join(failures).replace("|", "/")
        lines.append(f"| {row['id']} | {row['category']} | {mark} | {cells} |")
    return "\n".join(lines) + "\n"


def quiet_asyncgen_close(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    # HandoffBuilder runs copies of the agents, so their MCP sessions are finalized at loop
    # shutdown instead of by agent.close(); anyio then logs a harmless cancel scope error.
    if "closing of asynchronous generator" not in context.get("message", ""):
        loop.default_exception_handler(context)


async def collect(cases: list[dict[str, Any]], agents: Agents, parallel: int) -> list[dict]:
    asyncio.get_running_loop().set_exception_handler(quiet_asyncgen_close)
    gate = asyncio.Semaphore(parallel)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            trace = await agents.converse(
                case["turns"],
                customer=case.get("customer", CUSTOMER),
                approve=case.get("approve", True),
            )
            row = to_row(case, trace)
            print(f"[{row['id']}] {'ok' if not check(row) else check(row)}", flush=True)
            return row

    return list(await asyncio.gather(*(one(c) for c in cases)))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--name", default=None, help="nombre de la ejecución en Foundry")
    parser.add_argument("--limit", type=int, default=0, help="solo los primeros N casos")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--no-judges", action="store_true", help="solo checks deterministas")
    parser.add_argument("--no-upload", action="store_true", help="no registrar en Foundry")
    args = parser.parse_args()

    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    search = os.environ.get("SEARCH_ENDPOINT", "").strip()
    if not project or not search:
        print("Faltan FOUNDRY_PROJECT_ENDPOINT o SEARCH_ENDPOINT.", file=sys.stderr)
        return 2
    cases = [json.loads(line) for line in args.dataset.read_text("utf-8").splitlines() if line]
    cases = cases[: args.limit] if args.limit else cases
    name = args.name or f"{args.dataset.stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)

    with local_mcp() as mcp_url:
        rows = asyncio.run(collect(cases, Agents(project, search, mcp_url), args.parallel))
    rows_file = out_dir / "rows.jsonl"
    rows_file.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    result = None
    if not args.no_judges:
        model = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip() or "gpt-5-mini"
        result = evaluate(rows_file, out_dir, name, None if args.no_upload else project, model)
    text = summary(name, rows, result)
    (out_dir / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
