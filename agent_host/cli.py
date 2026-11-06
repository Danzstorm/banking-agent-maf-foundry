"""CLI for the Operaciones and Productos agents.

Interactive:   uv run python -m agent_host [--agent productos]
Scripted demo: uv run python -m agent_host --ask "¿Cuál es mi saldo?" [--auto-approve | --auto-deny]
               uv run python -m agent_host --agent productos --ask "¿Cuánto cuesta la Tarjeta Oro?"
"""

import argparse
import asyncio
import json
import sys
import warnings

from agent_framework import Content

from agent_host.agent import build_agent, build_foundry_client, chat_turn, new_session
from agent_host.config import ConfigError, load_productos_settings, load_settings
from agent_host.productos import ProductAnswer, ask, build_productos_agent

TOOL_LABELS = {
    "block_card": "Bloquear tarjeta",
    "open_claim": "Abrir reclamo",
}


def describe_request(req: Content) -> str:
    """Spanish description of an approval request: tool label, name and arguments."""
    call = req.function_call
    args = call.parse_arguments() or {}
    label = TOOL_LABELS.get(call.name, "Operación")
    shown = json.dumps(args, ensure_ascii=False) if args else "sin argumentos"
    return f"{label} ({call.name}) con {shown}"


def parse_answer(answer: str) -> bool:
    return answer.strip().lower() in {"s", "si", "sí", "y", "yes"}


def interactive_decide(req: Content) -> bool:
    print(f"\nEl agente quiere ejecutar: {describe_request(req)}")
    return parse_answer(input("¿Apruebas? (s/n): "))


def fixed_decide(approve: bool):
    def decide(req: Content) -> bool:
        print(f"[aprobación] {describe_request(req)} -> {'aprobada' if approve else 'rechazada'}")
        return approve

    return decide


def format_answer(answer: ProductAnswer) -> str:
    if not answer.sources:
        return answer.answer
    return f"{answer.answer}\nFuentes: {'; '.join(answer.sources)}"


async def run_productos(args: argparse.Namespace) -> None:
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    settings = load_productos_settings()
    async with DefaultAzureCredential() as credential:
        agent = build_productos_agent(
            build_foundry_client(settings, credential), settings, SyncCredential()
        )
        try:
            session = agent.create_session()
            print(f"Agente productos | base de conocimiento {settings.knowledge_base}")
            if args.ask:
                for question in args.ask:
                    print(f"\nTú: {question}")
                    print(f"Agente: {format_answer(await ask(agent, session, question))}")
                return
            print("Escribe tu consulta. Ctrl+C o 'salir' para terminar.")
            while True:
                text = (await asyncio.to_thread(input, "\nTú: ")).strip()
                if text.lower() in {"salir", "exit"}:
                    return
                if text:
                    print(f"Agente: {format_answer(await ask(agent, session, text))}")
        finally:
            await agent.close()


async def run(args: argparse.Namespace) -> None:
    from azure.identity.aio import DefaultAzureCredential

    if args.agent == "productos":
        await run_productos(args)
        return

    settings = load_settings()
    async with DefaultAzureCredential() as credential:
        # No `async with agent`: that connects MCP eagerly, before a run supplies the identity
        # headers. The first run connects lazily with them; close() releases the connection.
        agent = build_agent(build_foundry_client(settings, credential), settings)
        try:
            session = new_session(agent, settings.dev_customer_id)
            print(f"Cliente {settings.dev_customer_id} | conversación {session.session_id}")
            if args.ask:
                decide = fixed_decide(args.auto_approve)  # deny unless --auto-approve
                for question in args.ask:
                    print(f"\nTú: {question}")
                    response = await chat_turn(agent, session, question, decide)
                    print(f"Agente: {response.text}")
                return
            print("Escribe tu consulta. Ctrl+C o 'salir' para terminar.")
            while True:
                text = (await asyncio.to_thread(input, "\nTú: ")).strip()
                if text.lower() in {"salir", "exit"}:
                    return
                if text:
                    response = await chat_turn(agent, session, text, interactive_decide)
                    print(f"Agente: {response.text}")
        finally:
            await agent.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_host", description="Agentes de Banco Andino Demo.")
    parser.add_argument(
        "--agent", choices=["operaciones", "productos"], default="operaciones", help="Agente."
    )
    parser.add_argument("--ask", action="append", help="Pregunta no interactiva (repetible).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--auto-approve", action="store_true", help="Aprueba toda escritura.")
    mode.add_argument("--auto-deny", action="store_true", help="Rechaza toda escritura (defecto).")
    args = parser.parse_args(argv)
    # MAF 1.24 still sends MCP pings, deprecated in mcp 2.3; harmless, but noisy in a demo.
    warnings.filterwarnings("ignore", message="ping is removed")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    try:
        asyncio.run(run(args))
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
    return 0
