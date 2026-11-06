"""Triage workflow: HandoffBuilder over Triage, Productos and Operaciones (ADR-004).

Least privilege per agent:
- Triage: only `escalate_to_human` (plus the handoff tools the builder injects).
- Productos: the knowledge base and `escalate_to_human`; no banking tools.
- Operaciones: the banking MCP tools (including `escalate_to_human`); no knowledge base.

Identity travels as workflow-wide `function_invocation_kwargs`, exactly as in the single-agent
loop: the MCP `header_provider` reads it, the model never sees it. Approvals for writes surface
as `request_info` events carrying the `function_approval_request` and are answered with
`workflow.run(responses=...)`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agent_framework import Agent, SupportsChatGetResponse, Workflow, WorkflowEvent
from agent_framework.orchestrations import HandoffAgentUserRequest, HandoffBuilder

from agent_host.agent import CONVERSATION_KEY, CUSTOMER_KEY, ESCALATE_TOOL, Decide, build_mcp_tool
from agent_host.config import Settings

TRIAGE_INSTRUCTIONS = """\
Eres el agente de triage de Banco Andino Demo, un banco ficticio. Hablas en español neutro y \
tratas al cliente de tú. Tu único trabajo es decidir quién atiende: no respondes preguntas de \
productos ni de cuentas.

Reglas, en este orden:
1. Fraude, robo de tarjeta, cargos no reconocidos o suplantación: llama escalate_to_human con \
categoria "fraude". Fallecimiento del titular: categoria "fallecimiento". Demandas, abogados, \
denuncias o reclamos legales: categoria "legal". No pidas confirmación para escalar. Si el caso \
de fraude involucra una tarjeta, después de escalar transfiere a operaciones para que ofrezca \
bloquearla; si no, confirma al cliente que un asesor lo contactará.
2. Productos, tarifas, comisiones, requisitos o procesos del banco: transfiere a productos.
3. Datos u operaciones del propio cliente (saldo, movimientos, tarjetas, reclamos, bloqueos): \
transfiere a operaciones.
4. Si piden las dos cosas, transfiere a quien atiende la primera; ese agente puede transferir \
al otro.
5. Si no está claro qué necesita, haz una sola pregunta breve para aclararlo.
"""


def escalate_only_tool(settings: Settings):
    """The single MCP tool non-banking agents get: same server, filtered by `allowed_tools`."""
    return build_mcp_tool(settings, allowed_tools=[ESCALATE_TOOL])


def build_triage_agent(
    client: SupportsChatGetResponse,
    settings: Settings,
    *,
    middleware: Sequence[Any] | None = None,
) -> Agent:
    return Agent(
        client=client,
        name="triage",
        description="Clasifica la consulta y la deriva al agente correcto.",
        instructions=TRIAGE_INSTRUCTIONS,
        tools=escalate_only_tool(settings),
        default_options={"reasoning": {"effort": "low"}},
        middleware=middleware,
        require_per_service_call_history_persistence=True,
    )


def build_workflow(triage: Agent, productos: Agent, operaciones: Agent) -> Workflow:
    # Mesh topology (no add_handoff): every agent can transfer to every other one. A handoff
    # only moves the conversation; each agent keeps its own tools. The handoff tool the model
    # sees is described with the target agent's `description`.
    # enable_return_to_previous(False): every customer message goes back through Triage, so a
    # new topic is re-routed instead of landing on whoever spoke last (by default, Productos
    # would receive "¿y mi saldo?"). Approval answers still go to the agent that asked.
    return (
        HandoffBuilder(name="banco_andino", participants=[triage, productos, operaciones])
        .with_start_agent(triage)
        .enable_return_to_previous(False)
        .build()
    )


@dataclass
class Turn:
    """What happened in one customer turn."""

    replies: list[tuple[str, str]] = field(default_factory=list)  # (agent, text)
    handoffs: list[tuple[str, str]] = field(default_factory=list)  # (source, target)
    approvals: list[tuple[str, bool]] = field(default_factory=list)  # (tool, approved)

    @property
    def text(self) -> str:
        return "\n".join(text for _, text in self.replies)


class Conversation:
    """One customer conversation over the handoff workflow (one workflow per conversation)."""

    def __init__(self, workflow: Workflow, customer_id: int) -> None:
        self.workflow = workflow
        self.conversation_id = uuid4().hex
        self._kwargs = {CUSTOMER_KEY: customer_id, CONVERSATION_KEY: self.conversation_id}
        self._user_request_id: str | None = None  # pending HandoffAgentUserRequest

    async def send(self, text: str, decide: Decide) -> Turn:
        if self._user_request_id is None:
            result = await self.workflow.run(text, function_invocation_kwargs=self._kwargs)
        else:
            reply = HandoffAgentUserRequest.create_response(text)
            result = await self.workflow.run(
                responses={self._user_request_id: reply}, function_invocation_kwargs=self._kwargs
            )
        turn = Turn()
        while True:
            self._collect(result, turn)
            self._user_request_id = None
            answers: dict[str, Any] = {}
            for event in result.get_request_info_events():
                if isinstance(event.data, HandoffAgentUserRequest):
                    self._user_request_id = event.request_id
                elif getattr(event.data, "type", None) == "function_approval_request":
                    approved = decide(event.data)
                    turn.approvals.append((event.data.function_call.name, approved))
                    answers[event.request_id] = event.data.to_function_approval_response(approved)
            if not answers:
                return turn
            result = await self.workflow.run(
                responses=answers, function_invocation_kwargs=self._kwargs
            )

    @staticmethod
    def _collect(events: Sequence[WorkflowEvent], turn: Turn) -> None:
        for event in events:
            if event.type == "output" and getattr(event.data, "text", ""):
                turn.replies.append((event.executor_id, event.data.text))
            elif event.type == "handoff_sent":
                turn.handoffs.append((event.data.source, event.data.target))
