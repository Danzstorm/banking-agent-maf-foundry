"""Red teaming (phase 8): AI Red Teaming Agent (azure-ai-evaluation[redteam], preview) against
the triage workflow, wrapped as a callback. Single turn. Kept small on purpose: two risk
categories, two attack strategies plus the baseline, two objectives per category.

    uv run --group evals python -m evals.run_redteam

Same setup as evals/run_evals.py (local MCP, real model and knowledge base). The scan is logged
to the Foundry project (Foundry > Evaluations > AI red teaming). Our own adversarial cases live
in evals/adversarial_cases.jsonl and run with run_evals.
"""

import asyncio
import json
import os
import sys
import time
from typing import Any

from evals.run_evals import RESULTS, Agents, local_mcp, quiet_asyncgen_close, to_row


async def scan(project: str, agents: Agents, name: str) -> Any:
    from azure.ai.evaluation.red_team import AttackStrategy, RedTeam, RiskCategory
    from azure.identity import DefaultAzureCredential

    asyncio.get_running_loop().set_exception_handler(quiet_asyncgen_close)

    async def callback(messages, stream=False, session_state=None, context=None) -> dict:
        last = messages[-1]
        query = last["content"] if isinstance(last, dict) else last.content
        trace = await agents.converse([query], customer=1, approve=False)  # writes always denied
        text = to_row({"id": "rt", "category": "redteam", "turns": [query]}, trace)["response"]
        return {"messages": [{"role": "assistant", "content": text or "(sin respuesta)"}]}

    red_team = RedTeam(
        azure_ai_project=project,
        credential=DefaultAzureCredential(),
        risk_categories=[RiskCategory.Violence, RiskCategory.HateUnfairness],
        num_objectives=2,
        output_dir=str(RESULTS / name),
    )
    return await red_team.scan(
        target=callback,
        scan_name=name,
        attack_strategies=[AttackStrategy.Base64, AttackStrategy.Flip],
        output_path=str(RESULTS / name / "redteam.json"),
        max_parallel_tasks=2,
    )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    search = os.environ.get("SEARCH_ENDPOINT", "").strip()
    if not project or not search:
        print("Faltan FOUNDRY_PROJECT_ENDPOINT o SEARCH_ENDPOINT.", file=sys.stderr)
        return 2
    name = f"redteam-{time.strftime('%Y%m%d-%H%M%S')}"
    (RESULTS / name).mkdir(parents=True, exist_ok=True)
    with local_mcp() as mcp_url:
        result = asyncio.run(scan(project, Agents(project, search, mcp_url), name))
    scorecard = (result.scan_result or {}).get("scorecard", {})
    print(json.dumps(scorecard, ensure_ascii=False, indent=2, default=str))
    # The SDK reports "ASR 0.0%, scan completed" even when no attack could be scored (for
    # example, RAI scoring unavailable in the region): 0 of 0 is not a pass.
    totals = [s.get("overall_total", 0) for s in scorecard.get("risk_category_summary", [])]
    if not any(totals):
        print("Ningún ataque fue evaluado: revisa los errores del servicio RAI.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
