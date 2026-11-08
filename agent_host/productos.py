"""Productos agent: answers product questions only from the Foundry IQ knowledge base (ADR-012).

The knowledge base is reached through its MCP endpoint (tool `knowledge_base_retrieve`) with an
Entra bearer token for Azure AI Search. Retrieval is extractive; this agent synthesizes the
answer and returns it with its sources as structured output (ADR-018). It never sees customer
data: that lives only behind the Operaciones agent's MCP tools (ADR-003).
"""

import time
from collections.abc import Sequence
from typing import Any

import httpx
from agent_framework import Agent, AgentSession, MCPStreamableHTTPTool, SupportsChatGetResponse
from pydantic import BaseModel, Field

from agent_host.config import ProductosSettings

SEARCH_SCOPE = "https://search.azure.com/.default"
KB_TOOL = "knowledge_base_retrieve"
NO_INFO = "No tengo esa información."

INSTRUCTIONS = f"""\
Eres el asistente de productos de Banco Andino Demo, un banco ficticio. Hablas en español neutro \
y tratas al cliente de tú.

Reglas:
- Para toda pregunta sobre productos, tarifas, comisiones, requisitos o procesos del banco, llama \
primero a {KB_TOOL} con la pregunta completa del cliente, en sus propias palabras.
- Responde SOLO con información que aparece en los resultados de {KB_TOOL}. No uses conocimiento \
general ni completes cifras que no estén en los resultados.
- Copia las cifras tal como aparecen, con su moneda (S/) y su unidad.
- En `sources` pon el título de cada documento que usaste. El título es la primera línea del \
fragmento (por ejemplo "Tarifario de Comisiones y Gastos"); si el fragmento no empieza con un \
título, usa el nombre del archivo PDF de su referencia. Si respondes con información, incluye al \
menos una fuente.
- Si los resultados no contienen la respuesta, responde "{NO_INFO}" y sugiere al cliente hablar \
con un asesor de Banco Andino Demo. Deja `sources` vacío.
- No tienes acceso a datos personales del cliente: saldos, movimientos, tarjetas, reclamos ni \
datos de otras personas. Si te los piden, no llames a la herramienta: responde que no puedes \
consultarlos aquí y que el asistente de operaciones puede ayudarle. Deja `sources` vacío.
- Respuestas cortas: una a tres frases.
"""


class ProductAnswer(BaseModel):
    """Structured output of the Productos agent."""

    answer: str = Field(description="Respuesta al cliente, en español.")
    sources: list[str] = Field(
        default_factory=list,
        description="Títulos de los documentos usados. Vacío si no hay información.",
    )


class SearchTokenHeaders:
    """`header_provider` that adds an Entra bearer token (Azure AI Search by default).

    Takes a sync credential (MAF calls header providers synchronously) and caches the token
    until five minutes before it expires, so tool calls don't fetch one each time.
    """

    def __init__(self, credential: Any, refresh_margin_s: int = 300, scope: str = SEARCH_SCOPE):
        self._credential = credential
        self._margin = refresh_margin_s
        self._scope = scope
        self._token: Any = None

    def __call__(self, _kwargs: dict[str, Any]) -> dict[str, str]:
        if self._token is None or self._token.expires_on - self._margin <= time.time():
            self._token = self._credential.get_token(self._scope)
        return {"Authorization": f"Bearer {self._token.token}"}


def build_kb_tool(settings: ProductosSettings, credential: Any) -> MCPStreamableHTTPTool:
    return MCPStreamableHTTPTool(
        name="banco_andino_productos",
        url=settings.kb_mcp_url,
        description="Base de conocimiento de productos de Banco Andino Demo.",
        header_provider=SearchTokenHeaders(credential),
        approval_mode="never_require",  # read-only retrieval
        allowed_tools=[KB_TOOL],
        load_prompts=False,
        request_timeout=20,  # the circuit breaker (resilience.py) counts timeouts as failures
    )


DESCRIPTION = "Responde sobre productos, tarifas, requisitos y procesos del banco."
A2A_TOOL = "consultar_productos"

PROXY_INSTRUCTIONS = f"""\
Eres el asistente de productos de Banco Andino Demo, un banco ficticio. Hablas en español neutro \
y tratas al cliente de tú. No tienes conocimiento propio de productos: lo consultas a un servicio \
remoto.

Reglas:
- Para toda pregunta sobre productos, tarifas, comisiones, requisitos o procesos del banco, llama \
a {A2A_TOOL} con la pregunta del cliente reformulada para que se entienda sola (sin "eso" ni \
"la otra"), en español.
- El resultado es un JSON con `answer` y `sources`: copia ambos tal cual en tu respuesta. No \
agregues información que no esté en `answer`.
- Si el resultado dice que el servicio no está disponible, sigue sus indicaciones.
- No tienes acceso a datos personales del cliente: saldos, movimientos, tarjetas, reclamos ni \
datos de otras personas. Si te los piden, no llames a la herramienta: responde que no puedes \
consultarlos aquí y que el asistente de operaciones puede ayudarle. Deja `sources` vacío.
"""


class BearerAuth(httpx.Auth):
    """httpx auth that adds the managed-identity token for the Productos A2A app (ADR-015)."""

    def __init__(self, headers: SearchTokenHeaders) -> None:
        self._headers = headers

    def auth_flow(self, request: httpx.Request):
        # ponytail: the token is fetched with a sync credential on the event loop; it is cached
        # for its lifetime (~24 h for managed identities), so it blocks once a day per replica.
        request.headers.update(self._headers({}))
        yield request


def build_remote_productos(url: str, scope: str, credential: Any) -> tuple[Any, httpx.AsyncClient]:
    """The A2A client for the remote Productos agent, and the httpx client the caller closes.

    Built once per process and shared by every request: the httpx pool and the token cache
    outlive the per-request agents.
    """
    from agent_framework.a2a import A2AAgent

    http = httpx.AsyncClient(
        auth=BearerAuth(SearchTokenHeaders(credential, scope=scope)),
        # Scale from zero takes a few seconds; the answer itself (model + retrieval) up to ~30 s.
        timeout=httpx.Timeout(60, connect=15),
    )
    return A2AAgent(name="productos-a2a", description=DESCRIPTION, url=url, http_client=http), http


def build_productos_proxy(
    client: SupportsChatGetResponse,
    remote: Any,
    *,
    extra_tools: Sequence[Any] = (),
    middleware: Sequence[Any] | None = None,
) -> Agent:
    """Local stand-in for Productos inside the handoff workflow; answers through A2A.

    HandoffBuilder only accepts local `Agent` participants, so the remote agent is one tool of
    this proxy. Guard that tool with `ResilientTools(breaker, guarded=A2A_TOOL)`.
    """
    tool = remote.as_tool(
        name=A2A_TOOL,
        description="Consulta la base de conocimiento de productos de Banco Andino Demo.",
        arg_name="pregunta",
        arg_description="Pregunta del cliente, completa y en español.",
    )
    return Agent(
        client=client,
        name="productos",
        description=DESCRIPTION,
        instructions=PROXY_INSTRUCTIONS,
        tools=[tool, *extra_tools],
        default_options={"reasoning": {"effort": "low"}, "response_format": ProductAnswer},
        middleware=middleware,
        require_per_service_call_history_persistence=True,
    )


def build_productos_agent(
    client: SupportsChatGetResponse,
    settings: ProductosSettings,
    search_credential: Any,
    *,
    extra_tools: Sequence[Any] = (),
    middleware: Sequence[Any] | None = None,
) -> Agent:
    """`extra_tools`: only `escalate_to_human` in the triage workflow; never banking tools."""
    return Agent(
        client=client,
        name="productos",
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        tools=[build_kb_tool(settings, search_credential), *extra_tools],
        # ADR-002, ADR-018. response_format here too so it holds inside the handoff workflow,
        # where the caller can't pass per-run options.
        default_options={"reasoning": {"effort": "low"}, "response_format": ProductAnswer},
        middleware=middleware,
        require_per_service_call_history_persistence=True,  # required by HandoffBuilder
    )


async def ask(agent: Agent, session: AgentSession, text: str) -> ProductAnswer:
    response = await agent.run(text, session=session, options={"response_format": ProductAnswer})
    return response.value
