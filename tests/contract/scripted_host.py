"""A real agent host process (HTTP service) with scripted models, for the chaos tests. No Azure.

uv run python -m tests.contract.scripted_host <script>
env: MCP_URL, DATABASE_URL, DEV_CUSTOMER_ID, AGENT_PORT
"""

import asyncio
import os
import sys

import uvicorn
from agent_framework import Message

from agent_host.agent import build_agent
from agent_host.config import ProductosSettings, Settings
from agent_host.productos import build_productos_agent
from agent_host.service import Host, create_app, dev_authenticator
from agent_host.sessions import sessions_engine
from agent_host.workflow import build_triage_agent, escalate_only_tool
from tests.contract.test_agent_loop import ScriptedClient, call

HANG = "hang"  # script step: the model call never returns (the replica dies meanwhile)


class HangingClient(ScriptedClient):
    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        if self.script and self.script[0] == HANG:

            async def hang():
                await asyncio.sleep(3600)

            return hang()
        return super()._inner_get_response(
            messages=messages, stream=stream, options=options, **kwargs
        )


def scripts(card_id: int) -> dict[str, dict[str, list]]:
    propose = {
        "triage": [call("handoff_to_operaciones")],
        "operaciones": [call("block_card", card_id=card_id)],
    }
    return {
        "propose": propose,
        "propose_then_hang": {
            "triage": propose["triage"],
            "operaciones": [*propose["operaciones"], HANG],
        },
        "finish": {"operaciones": [Message("assistant", ["Listo."])]},
    }


def main() -> None:
    customer = int(os.environ["DEV_CUSTOMER_ID"])
    script = scripts(customer)[sys.argv[1]]  # fixture: card id == customer id
    settings = Settings("https://unused.example", "scripted", os.environ["MCP_URL"], customer)
    kb = ProductosSettings("https://unused.example", "scripted", "https://kb.example", "kb")
    clients = {
        name: HangingClient(*script.get(name, []))
        for name in ("triage", "productos", "operaciones")
    }

    def participants():
        return [
            build_triage_agent(clients["triage"], settings),
            build_productos_agent(
                clients["productos"], kb, None, extra_tools=[escalate_only_tool(settings)]
            ),
            build_agent(clients["operaciones"], settings),
        ]

    host = Host(
        engine=sessions_engine(os.environ["DATABASE_URL"]),
        authenticate=dev_authenticator(settings),
        build_participants=participants,
    )
    uvicorn.run(create_app(host), host="127.0.0.1", port=int(os.environ["AGENT_PORT"]))


if __name__ == "__main__":
    main()
