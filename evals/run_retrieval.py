"""Retrieval evaluation: does the knowledge base return the expected source document?

Calls the knowledge base retrieve action directly (no agent, no LLM synthesis), so it measures
retrieval alone. Answer quality is evaluated separately (tests/scenarios, ADR-018).

    export SEARCH_ENDPOINT=$(terraform -chdir=infra output -raw search_endpoint)
    uv run python evals/run_retrieval.py

hit@1: the expected document is the top reference. hit@k: it is among all references returned.
A case lists several expected documents when the answer legitimately lives in more than one.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from azure.identity import DefaultAzureCredential

CASES = Path(__file__).with_name("retrieval_cases.jsonl")
API_VERSION = "2026-04-01"


def doc_name(blob_url: str) -> str:
    """https://acct.blob.core.windows.net/productos/cuenta-corriente.pdf -> cuenta-corriente"""
    return Path(urlparse(blob_url).path).stem


def ranked_docs(references: list[dict]) -> list[str]:
    """Document names in reference order, without duplicates (several chunks per document)."""
    names: list[str] = []
    for ref in references:
        name = doc_name(ref.get("blobUrl", ""))
        if name and name not in names:
            names.append(name)
    return names


def score(expected: list[str], ranked: list[str]) -> tuple[bool, bool]:
    return (bool(ranked) and ranked[0] in expected, any(d in expected for d in ranked))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    endpoint = os.environ.get("SEARCH_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        print("Falta SEARCH_ENDPOINT.", file=sys.stderr)
        return 2
    kb = os.environ.get("KNOWLEDGE_BASE", "kb-productos")
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]
    credential = DefaultAzureCredential()
    hits1 = hitsk = 0
    with httpx.Client(timeout=60) as client:
        for case in cases:
            token = credential.get_token("https://search.azure.com/.default").token
            resp = client.post(
                f"{endpoint}/knowledgebases/{kb}/retrieve",
                params={"api-version": API_VERSION},
                headers={"Authorization": f"Bearer {token}"},
                json={"intents": [{"type": "semantic", "search": case["question"]}]},
            )
            resp.raise_for_status()
            ranked = ranked_docs(resp.json().get("references", []))
            h1, hk = score(case["expected"], ranked)
            hits1 += h1
            hitsk += hk
            mark = "OK " if h1 else ("k  " if hk else "MISS")
            print(
                f"{mark} {case['question']}\n     esperado {case['expected']} | obtenido {ranked}"
            )
    n = len(cases)
    print(f"\nhit@1 = {hits1}/{n} ({hits1 / n:.0%}) | hit@k = {hitsk}/{n} ({hitsk / n:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
