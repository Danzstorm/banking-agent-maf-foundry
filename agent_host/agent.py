"""Operaciones agent: MAF loop in our process, gpt-5-mini on Foundry, tools from the MCP server.

Identity never comes from the model. The customer identity and conversation id live in the
`AgentSession` (host state), travel as `function_invocation_kwargs` on every run, and the MCP
tool's `header_provider` turns them into HTTP headers. MAF hands `header_provider` only that
host runtime context, never model-generated arguments. The identity is either a dev customer id
(AUTH_MODE=dev) or a callable that returns the customer's OBO token (AUTH_MODE=entra).
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
    register_checkpoint_type,
)

from agent_host.config import Settings

READ_TOOLS = ["get_balance", "list_transactions", "get_claim_status"]
WRITE_TOOLS = ["block_card", "open_claim"]
# A request for help, not an operation on the customer's products: no approval (ADR-004).
ESCALATE_TOOL = "escalate_to_human"

# Keys of the host runtime context (function_invocation_kwargs). Not visible to the model.
CUSTOMER_KEY = "mcp_identity"
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
    identity = kwargs[CUSTOMER_KEY]
    if callable(identity):
        auth = {"Authorization": f"Bearer {identity()}"}
    else:
        auth = {"X-Dev-Customer-Id": str(int(identity))}
    return auth | {"X-Conversation-Id": str(kwargs[CONVERSATION_KEY])}


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
    """FoundryChatClient over the Responses API, authenticated with an Entra credential.

    Resilience (ADR-014): 60 s per model call, and our retry policy (backoff + jitter on
    429/5xx) as client-level chat middleware. The OpenAI SDK's own 2 retries are turned off so
    there is one policy, not retries multiplied by retries.
    """
    from agent_framework.foundry import FoundryChatClient

    from agent_host.resilience import RetryChatMiddleware

    client = FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=settings.model_deployment,
        credential=credential,
        middleware=[RetryChatMiddleware()],
        timeout=60,
    )
    client.client = client.client.with_options(max_retries=0)
    return client


class TokenProvider:
    """Bearer token callable for the MCP `header_provider` (AUTH_MODE=entra in the HTTP host).

    Workflow checkpoints persist the run kwargs, identity included (ADR-013). This pickles as
    an empty provider, so no token or credential ever reaches `sesiones`; every run passes a
    fresh one, which replaces the restored value.
    """

    def __init__(self, get_token: Callable[[], str] | None = None) -> None:
        self._get_token = get_token

    def __call__(self) -> str:
        if self._get_token is None:
            raise RuntimeError("Token provider restored from a checkpoint; pass a fresh one.")
        return self._get_token()

    def __reduce__(self):
        return (TokenProvider, ())


register_checkpoint_type(TokenProvider)

Identity = int | Callable[[], str]  # dev customer id, or a bearer token provider


def new_session(agent: Agent, identity: Identity) -> AgentSession:
    """One session per conversation; its random session_id is the conversation id."""
    session = agent.create_session()
    session.state[CUSTOMER_KEY] = identity
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
