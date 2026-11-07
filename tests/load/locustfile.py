"""Phase 6 load test against the deployed agent host (real Foundry, real MCP, real SQL).

Signs in once by device code (same variables as `agent_host --remote`), then every simulated
user asks read-only questions with that customer's token. No writes: approvals never pause.

    AUTH_MODE=entra ENTRA_AUTHORITY=... ENTRA_CLI_CLIENT_ID=... ENTRA_AGENT_API_CLIENT_ID=... \
    uv run --group load locust -f tests/load/locustfile.py --headless -u 3 -r 1 -t 3m \
        --host https://<agent-host> --csv docs/load/fase-6

All users share one customer, so the per-user rate limit (20/min, burst 10) is part of what is
measured: 429 answers are counted apart from real failures.
"""

import random

from locust import HttpUser, between, events, task

from agent_host.cli import remote_auth

QUESTIONS = [
    "¿Cuál es el saldo de mis cuentas?",
    "¿Cuáles son mis últimos movimientos?",
    "¿Cuánto cuesta la Tarjeta Oro?",
    "¿Qué requisitos tiene la cuenta de ahorros?",
]

identity = None


@events.test_start.add_listener
def sign_in(environment, **_):
    global identity
    identity, who = remote_auth()
    if identity is None:
        raise SystemExit("AUTH_MODE=entra es obligatorio contra el host desplegado.")
    print(who)


class Customer(HttpUser):
    wait_time = between(3, 8)

    @task
    def ask(self):
        headers = {"Authorization": f"Bearer {identity.user_token()}"}
        body = {"message": random.choice(QUESTIONS)}  # noqa: S311
        with self.client.post(
            "/chat", json=body, headers=headers, timeout=150, catch_response=True
        ) as response:
            if response.status_code == 429:
                response.failure("rate limited (429)")
            elif response.status_code != 200 or not response.json().get("replies"):
                response.failure(f"HTTP {response.status_code}")
