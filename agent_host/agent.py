"""Operaciones agent: MAF loop in our process, gpt-5-mini on Foundry, tools from the MCP server.

Identity never comes from the model. The customer id and conversation id live in the
`AgentSession` (host state), travel as `function_invocation_kwargs` on every run, and the MCP
tool's `header_provider` turns them into HTTP headers. MAF hands `header_provider` only that
host runtime context, never model-generated arguments.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    MCPStreamableHTTPTool,
    Message,
    SupportsChatGetResponse,
)

from agent_host.config import Settings

READ_TOOLS = ["get_balance", "list_transactions", "get_claim_status"]
WRITE_TOOLS = ["block_card", "open_claim"]
# A request for help, not an operation on the customer's products: no approval (ADR-004).
ESCALATE_TOOL = "escalate_to_human"

# Keys of the host runtime context (function_invocation_kwargs). Not visible to the model.
CUSTOMER_KEY = "dev_customer_id"
# "conversation_id" itself is reserved: MAF strips it from the kwargs that reach tools.
CONVERSATION_KEY = "mcp_conversation_id"

INSTRUCTIONS = """\
Eres el asistente de operaciones de Banco Andino Demo, un banco ficticio. Atiendes al cliente \
autenticado en esta conversación. Hablas en español neutro y lo tratas de tú.

Reglas:
- Los datos del cliente (saldos, tarjetas, movimientos, reclamos) salen solo de tus herramientas. \
Nunca inventes cifras, fechas, números de tarjeta ni estados.
- Muestra los montos con la moneda que devuelve la herramienta, sin convertirlos.
- Solo atiendes al cliente autenticado. Si te piden datos u operaciones de otra persona o de otro \
número de cliente, rechaza con amabilidad y ofrece ayudar con su propia información. Tus \
herramientas no aceptan un id de cliente: no intentes pasarlo.
- Bloquear una tarjeta y abrir un reclamo son operaciones de escritura. Si no está claro qué \
tarjeta o qué motivo, pregunta primero. Si está claro, llama la herramienta sin pedir confirmación \
por texto: el sistema le pide al cliente su aprobación explícita antes de ejecutarla. Para \
bloquear, usa el card_id que devuelve get_balance.
- Si el resultado de una operación dice "rejected by user", el cliente no la aprobó: \
responde que no se realizó y no la vuelvas a pedir ni preguntes de nuevo.
- Si no tienes la información o una herramienta falla, responde "no tengo esa información" y no \
supongas.
- Respuestas cortas: una o dos frases, o una lista breve.
- Hoy es {today}. Las fechas de las herramientas usan el formato AAAA-MM-DD.
"""


def mcp_headers(kwargs: Mapping[str, Any]) -> dict[str, str]:
    """`header_provider` for the MCP tool: identity from host runtime context only.

    A missing key raises KeyError, which MAF tolerates only for connections made outside a run.
    """
    return {
        "X-Dev-Customer-Id": str(int(kwargs[CUSTOMER_KEY])),
        "X-Conversation-Id": str(kwargs[CONVERSATION_KEY]),
    }


def build_mcp_tool(
    settings: Settings, allowed_tools: Sequence[str] | None = None
) -> MCPStreamableHTTPTool:
    """MCP banking tools. `allowed_tools` narrows what the agent sees (least privilege)."""
    return MCPStreamableHTTPTool(
        name="banco_andino_core",
        url=settings.mcp_url,
        description="Core bancario de Banco Andino Demo.",
        header_provider=mcp_headers,
        # ADR-006: anything not explicitly listed as a read requires approval.
        approval_mode={
            "default": "always_require",
            "always_require_approval": WRITE_TOOLS,
            "never_require_approval": [*READ_TOOLS, ESCALATE_TOOL],
        },
        allowed_tools=allowed_tools,
        load_prompts=False,
        request_timeout=30,
    )


def build_agent(
    client: SupportsChatGetResponse,
    settings: Settings,
    *,
    middleware: Sequence[Any] | None = None,
) -> Agent:
    return Agent(
        client=client,
        name="operaciones",
        description="Consulta y opera sobre las cuentas, tarjetas y reclamos del cliente.",
        instructions=INSTRUCTIONS.format(today=date.today().isoformat()),
        tools=build_mcp_tool(settings),
        # gpt-5-mini is a reasoning model: no temperature; pin the effort (ADR-002, ADR-018).
        default_options={"reasoning": {"effort": "low"}},
        middleware=middleware,
        # Required by HandoffBuilder: handoff tool calls are short-circuited by middleware.
        require_per_service_call_history_persistence=True,
    )


def build_foundry_client(settings: Settings, credential: Any):
    """FoundryChatClient over the Responses API, authenticated with an Entra credential."""
    from agent_framework.foundry import FoundryChatClient

    return FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=settings.model_deployment,
        credential=credential,
    )


def new_session(agent: Agent, customer_id: int) -> AgentSession:
    """One session per conversation; its random session_id is the conversation id."""
    session = agent.create_session()
    session.state[CUSTOMER_KEY] = customer_id
    return session


def run_kwargs(session: AgentSession) -> dict[str, Any]:
    return {CUSTOMER_KEY: session.state[CUSTOMER_KEY], CONVERSATION_KEY: session.session_id}


Decide = Callable[[Content], bool]


async def chat_turn(
    agent: Agent, session: AgentSession, text: str, decide: Decide
) -> AgentResponse:
    """One user turn. Every approval request is resolved by `decide` before continuing."""
    response = await agent.run(
        text, session=session, function_invocation_kwargs=run_kwargs(session)
    )
    while response.user_input_requests:
        answers = [
            req.to_function_approval_response(decide(req)) for req in response.user_input_requests
        ]
        response = await agent.run(
            Message("user", answers),
            session=session,
            function_invocation_kwargs=run_kwargs(session),
        )
    return response


class ToolCallRecorder(FunctionMiddleware):
    """Records every tool call that actually executes: (name, arguments). Used by tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        self.calls.append((context.function.name, dict(context.arguments or {})))
        await call_next()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]
