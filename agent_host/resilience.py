"""Resilience layer (ADR-014): retries with exponential backoff and jitter, and a circuit breaker.

MAF has no retry or circuit breaker of its own, so:
- `RetryChatMiddleware` retries model calls on 429/5xx/timeouts (client-level chat middleware).
  The OpenAI SDK's own retries are turned off in `build_foundry_client`, so this is the only
  retry policy and its numbers are the real ones.
- `ResilientTools` retries tool calls on transport errors. Retrying MCP writes is safe only
  because they are idempotent (ADR-007). With a `breaker`, it also guards one tool: after
  `threshold` consecutive failures the tool is not called for `reset_after_s` seconds and the
  model gets a fallback that tells it to offer a human advisor instead of hanging.
- `db_retry` retries SQL operations: Azure SQL free offer auto-pauses and the first connection
  fails with 40613 while it resumes.
"""

import time
from collections.abc import Iterator

import httpx
import openai
from agent_framework import (
    ChatContext,
    ChatMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
)
from sqlalchemy.exc import DBAPIError
from tenacity import (
    AsyncRetrying,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Azure SQL transient errors: database unavailable/resuming, throttling, failover.
SQL_TRANSIENT_CODES = ("40613", "40197", "40501", "49918", "49919", "49920", "10928", "10929")


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and its causes; MAF wraps provider errors (`inner_exception`/`__cause__`)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "inner_exception", None) or current.__cause__


def is_transient(exc: BaseException) -> bool:
    """True for failures worth retrying: throttling, server errors, timeouts, dropped links."""
    for err in _chain(exc):
        if isinstance(err, openai.APIStatusError):
            return err.status_code in (408, 429) or err.status_code >= 500
        if isinstance(err, openai.APIConnectionError | httpx.TransportError | TimeoutError):
            return True
        if isinstance(err, DBAPIError):
            return err.connection_invalidated or any(c in str(err) for c in SQL_TRANSIENT_CODES)
    return False


def _policy(attempts: int, initial_s: float, max_wait_s: float, sleep=None) -> dict:
    policy = {
        # initial * 2^n plus up to 1 s of random jitter, so retrying replicas don't sync up.
        "wait": wait_exponential_jitter(initial=initial_s, max=max_wait_s, jitter=1),
        "stop": stop_after_attempt(attempts),
        "retry": retry_if_exception(is_transient),
        "reraise": True,
    }
    return policy if sleep is None else policy | {"sleep": sleep}


def db_retry(attempts: int = 6, sleep=None) -> Retrying:
    """Sync retry for SQL: waits ~1, 2, 4, 8, 16 s (+jitter), enough for an auto-paused
    Azure SQL free-offer database to resume (error 40613 meanwhile)."""
    return Retrying(**_policy(attempts, 1, 20, sleep))


class RetryChatMiddleware(ChatMiddleware):
    """Retries a non-streaming model call on transient errors (429/5xx/timeouts)."""

    def __init__(self, attempts: int = 4, sleep=None) -> None:
        self.attempts = attempts
        self.sleep = sleep  # tests inject a recorder

    async def process(self, context: ChatContext, call_next) -> None:
        if context.stream:  # errors surface while iterating the stream, not here
            await call_next()
            return
        async for attempt in AsyncRetrying(**_policy(self.attempts, 1, 8, self.sleep)):
            with attempt:
                await call_next()


class CircuitBreaker:
    """Counter-based breaker: opens after `threshold` consecutive failures, half-opens later.

    ponytail: state lives in the process, so each replica learns separately. Fine for two
    replicas; share it (Redis, SQL) only if many replicas hammering a dead dependency matters.
    """

    def __init__(self, threshold: int = 3, reset_after_s: float = 30, clock=time.monotonic):
        self.threshold = threshold
        self.reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half-open" if self._clock() - self._opened_at >= self.reset_after_s else "open"

    def allow(self) -> bool:
        """Closed or half-open (one trial call after the cool-down) lets the call through."""
        return self.state != "open"

    def success(self) -> None:
        self._failures, self._opened_at = 0, None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold or self.state == "half-open":
            self._opened_at = self._clock()


BREAKER_FALLBACK = (
    "La base de conocimiento no está disponible en este momento. No respondas con datos de "
    "productos: dile al cliente que no puedes consultarla ahora y ofrécele hablar con un asesor "
    "(escalate_to_human)."
)


class ResilientTools(FunctionMiddleware):
    """Retries tool calls on transient errors; with `breaker`, guards the tool `guarded`."""

    def __init__(
        self,
        breaker: CircuitBreaker | None = None,
        guarded: str | None = None,
        attempts: int = 3,
        sleep=None,
    ) -> None:
        self.breaker = breaker
        self.guarded = guarded
        self.attempts = attempts
        self.sleep = sleep

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        breaker = self.breaker if context.function.name == self.guarded else None
        if breaker is not None and not breaker.allow():
            context.result = BREAKER_FALLBACK
            return
        try:
            async for attempt in AsyncRetrying(**_policy(self.attempts, 0.5, 4, self.sleep)):
                with attempt:
                    await call_next()
        except Exception:
            if breaker is None:
                raise  # MAF turns it into a tool-error result for the model
            breaker.failure()
            context.result = BREAKER_FALLBACK
            return
        if breaker is not None:
            breaker.success()
