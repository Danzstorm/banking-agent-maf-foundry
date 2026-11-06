"""Productos agent: answers product questions only from the Foundry IQ knowledge base (ADR-012).

The knowledge base is reached through its MCP endpoint (tool `knowledge_base_retrieve`) with an
Entra bearer token for Azure AI Search. Retrieval is extractive; this agent synthesizes the
answer and returns it with its sources as structured output (ADR-018). It never sees customer
data: that lives only behind the Operaciones agent's MCP tools (ADR-003).
"""

import time
from collections.abc import Sequence
from typing import Any

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
    """`header_provider` that adds an Entra bearer token for Azure AI Search.

    Takes a sync credential (MAF calls header providers synchronously) and caches the token
    until five minutes before it expires, so tool calls don't fetch one each time.
    """

    def __init__(self, credential: Any, refresh_margin_s: int = 300) -> None:
        self._credential = credential
        self._margin = refresh_margin_s
        self._token: Any = None

    def __call__(self, _kwargs: dict[str, Any]) -> dict[str, str]:
        if self._token is None or self._token.expires_on - self._margin <= time.time():
            self._token = self._credential.get_token(SEARCH_SCOPE)
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
        request_timeout=60,
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
        description="Responde sobre productos, tarifas, requisitos y procesos del banco.",
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
