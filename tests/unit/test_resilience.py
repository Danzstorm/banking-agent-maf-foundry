"""Resilience layer (ADR-014): retries with backoff + jitter and the circuit breaker."""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest
from agent_framework.exceptions import ChatClientException
from sqlalchemy.exc import OperationalError

from agent_host.resilience import (
    BREAKER_FALLBACK,
    CircuitBreaker,
    ResilientTools,
    RetryChatMiddleware,
    db_retry,
    is_transient,
)

REQUEST = httpx.Request("POST", "https://foundry.example/openai/responses")


def status_error(status: int) -> openai.APIStatusError:
    cls = openai.RateLimitError if status == 429 else openai.APIStatusError
    return cls("boom", response=httpx.Response(status, request=REQUEST), body=None)


def wrapped(exc: Exception) -> ChatClientException:
    """How MAF's Foundry client surfaces provider errors."""
    try:
        raise ChatClientException("service failed", inner_exception=exc) from exc
    except ChatClientException as wrapper:
        return wrapper


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_throttling_and_server_errors_are_transient(status):
    assert is_transient(wrapped(status_error(status)))


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    assert not is_transient(wrapped(status_error(status)))


def test_sql_paused_database_is_transient():
    paused = OperationalError("connect", {}, Exception("Error 40613: Database is not available"))
    assert is_transient(paused)
    assert not is_transient(OperationalError("x", {}, Exception("Error 208: invalid object")))


def test_simulated_429_is_retried_with_exponential_backoff_and_jitter():
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def call_next() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise wrapped(status_error(429))

    middleware = RetryChatMiddleware(attempts=4, sleep=record)
    asyncio.run(middleware.process(SimpleNamespace(stream=False), call_next))

    assert calls == 3
    # initial 1 s, doubled each time, plus up to 1 s of jitter.
    assert len(sleeps) == 2
    assert 1 <= sleeps[0] <= 2
    assert 2 <= sleeps[1] <= 3


def test_retries_stop_after_the_attempt_budget():
    calls = 0

    async def call_next() -> None:
        nonlocal calls
        calls += 1
        raise wrapped(status_error(503))

    async def no_sleep(_s: float) -> None:
        pass

    with pytest.raises(ChatClientException):
        asyncio.run(
            RetryChatMiddleware(attempts=3, sleep=no_sleep).process(
                SimpleNamespace(stream=False), call_next
            )
        )
    assert calls == 3


def test_bad_request_is_not_retried():
    calls = 0

    async def call_next() -> None:
        nonlocal calls
        calls += 1
        raise wrapped(status_error(400))

    with pytest.raises(ChatClientException):
        asyncio.run(RetryChatMiddleware().process(SimpleNamespace(stream=False), call_next))
    assert calls == 1


def test_db_retry_rides_out_an_auto_paused_database():
    attempts = 0

    def connect() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("connect", {}, Exception("40613 Database is not available"))
        return "ok"

    result = None
    for attempt in db_retry(sleep=lambda _s: None):
        with attempt:
            result = connect()
    assert (result, attempts) == ("ok", 3)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    clock = Clock()
    breaker = CircuitBreaker(threshold=3, reset_after_s=30, clock=clock)
    for _ in range(2):
        breaker.failure()
    assert breaker.state == "closed"
    breaker.failure()
    assert (breaker.state, breaker.allow()) == ("open", False)
    clock.now = 31
    assert (breaker.state, breaker.allow()) == ("half-open", True)
    breaker.failure()  # the trial call failed: open again
    assert breaker.state == "open"
    clock.now = 62
    breaker.success()
    assert breaker.state == "closed"


def tool_context(name: str):
    return SimpleNamespace(function=SimpleNamespace(name=name), result=None)


def test_open_breaker_answers_with_escalation_offer_without_calling_the_tool():
    async def no_sleep(_s: float) -> None:
        pass

    breaker = CircuitBreaker(threshold=2, reset_after_s=60)
    middleware = ResilientTools(breaker, guarded="knowledge_base_retrieve", sleep=no_sleep)
    calls = 0

    async def dead_kb() -> None:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("knowledge base unreachable")

    async def run() -> list:
        results = []
        for _ in range(3):
            ctx = tool_context("knowledge_base_retrieve")
            await middleware.process(ctx, dead_kb)
            results.append(ctx.result)
        return results

    results = asyncio.run(run())
    assert results == [BREAKER_FALLBACK] * 3
    assert "asesor" in BREAKER_FALLBACK
    assert calls == 2 * 3  # two failing turns x 3 attempts; the third never reaches the tool
    assert breaker.state == "open"


def test_unguarded_tools_retry_transport_errors_then_succeed():
    async def no_sleep(_s: float) -> None:
        pass

    calls = 0

    async def flaky_mcp() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("mcp")

    ctx = tool_context("block_card")
    asyncio.run(ResilientTools(sleep=no_sleep).process(ctx, flaky_mcp))
    assert calls == 2
