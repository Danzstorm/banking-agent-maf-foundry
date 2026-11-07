"""Chaos: kill an agent host replica mid-conversation; another replica finishes it. No Azure.

Two real host processes (scripted models) share the SQLite database with the real MCP server
process, as two Container Apps replicas share Azure SQL. `kill()` is SIGKILL/TerminateProcess:
no shutdown hook runs, so whatever survives is what was already in `sesiones`.
"""

import os
import subprocess
import sys
import threading
import time
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select

from mcp_server.db import audit_log, cards, idempotency_keys
from tests.conftest import CARD_A, CARD_B, ROOT, A, B, _free_port

pytestmark = pytest.mark.contract


def start_host(script: str, mcp_url: str, db_url: str, customer: int):
    port = _free_port()
    env = os.environ | {
        "MCP_URL": mcp_url,
        "DATABASE_URL": db_url,
        "DEV_CUSTOMER_ID": str(customer),
        "AGENT_PORT": str(port),
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "tests.contract.scripted_host", script], cwd=ROOT, env=env
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        with suppress(httpx.HTTPError):
            if httpx.get(f"{base}/readyz", timeout=1).status_code == 200:
                return proc, base
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail("agent host did not start")
        time.sleep(0.2)


def chat(base: str, **body) -> httpx.Response:
    return httpx.post(f"{base}/chat", json=body, timeout=60)


def kill(proc) -> None:
    proc.kill()
    proc.wait(timeout=10)


def approve_all(response: httpx.Response) -> list[dict]:
    return [
        {"request_id": p["request_id"], "approved": True}
        for p in response.json()["pending_approvals"]
    ]


def test_pending_approval_survives_killing_the_replica(server):
    mcp_url, engine = server
    a, base_a = start_host("propose", mcp_url, str(engine.url), A)
    try:
        first = chat(base_a, message="Bloquea mi tarjeta")
    finally:
        kill(a)
    assert first.status_code == 200
    pending = first.json()["pending_approvals"]
    assert [(p["tool"], p["args"]) for p in pending] == [("block_card", {"card_id": CARD_A})]
    conversation_id = first.json()["conversation_id"]

    b, base_b = start_host("finish", mcp_url, str(engine.url), A)
    try:
        done = chat(base_b, conversation_id=conversation_id, approvals=approve_all(first))
        replay = chat(base_b, conversation_id=conversation_id, approvals=approve_all(first))
    finally:
        kill(b)
    assert done.status_code == 200
    assert (done.json()["text"], done.json()["pending_approvals"]) == ("Listo.", [])
    assert replay.status_code == 409  # that approval is no longer pending: nothing re-runs
    with engine.connect() as conn:
        assert conn.scalar(select(cards.c.estado).where(cards.c.id == CARD_A)) == "bloqueada"
        calls = conn.scalar(
            select(func.count())
            .select_from(audit_log)
            .where(audit_log.c.conversation_id == conversation_id, audit_log.c.tool == "block_card")
        )
    assert calls == 1


def test_replica_killed_while_the_approved_write_runs_completes_it_once(server):
    mcp_url, engine = server
    a, base_a = start_host("propose_then_hang", mcp_url, str(engine.url), B)
    try:
        first = chat(base_a, message="Bloquea mi tarjeta")
        conversation_id = first.json()["conversation_id"]

        def block_card_calls() -> int:
            with engine.connect() as conn:
                return conn.scalar(
                    select(func.count())
                    .select_from(audit_log)
                    .where(
                        audit_log.c.conversation_id == conversation_id,
                        audit_log.c.tool == "block_card",
                    )
                )

        # The approval reaches replica A, block_card runs, then A hangs on the next model call.
        def approve_on_a() -> None:
            with suppress(httpx.HTTPError):  # A dies before answering
                chat(base_a, conversation_id=conversation_id, approvals=approve_all(first))

        threading.Thread(target=approve_on_a, daemon=True).start()
        deadline = time.monotonic() + 20
        while block_card_calls() == 0:
            assert time.monotonic() < deadline, "block_card never ran on replica A"
            time.sleep(0.1)
        with engine.connect() as conn:
            keys_before = conn.scalar(select(func.count()).select_from(idempotency_keys))
    finally:
        kill(a)  # mid-turn: the checkpoint after the write was never saved

    # The client retries the same approval against another replica.
    b, base_b = start_host("finish", mcp_url, str(engine.url), B)
    try:
        done = chat(base_b, conversation_id=conversation_id, approvals=approve_all(first))
    finally:
        kill(b)
    assert done.status_code == 200
    assert (done.json()["text"], done.json()["pending_approvals"]) == ("Listo.", [])
    with engine.connect() as conn:
        assert conn.scalar(select(cards.c.estado).where(cards.c.id == CARD_B)) == "bloqueada"
        keys_after = conn.scalar(select(func.count()).select_from(idempotency_keys))
    # Replica B re-ran the interrupted call (2 calls reached MCP) but the write happened once.
    assert block_card_calls() == 2
    assert keys_after == keys_before
