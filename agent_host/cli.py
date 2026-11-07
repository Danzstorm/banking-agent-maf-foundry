"""CLI for Banco Andino Demo. Default: the triage workflow (Triage -> Productos | Operaciones).

Interactive:   uv run python -m agent_host [--agent triage|operaciones|productos]
Scripted demo: uv run python -m agent_host --ask "¿Cuál es mi saldo?" [--auto-approve | --auto-deny]
               uv run python -m agent_host --agent productos --ask "¿Cuánto cuesta la Tarjeta Oro?"
Remote host:   uv run python -m agent_host --remote https://<agent-host> [--ask ...]
               (thin client of the HTTP service; AUTH_MODE=entra signs in by device code)
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import warnings

from agent_framework import Content

from agent_host.agent import Identity, build_agent, build_foundry_client, chat_turn, new_session
from agent_host.config import (
    ConfigError,
    EntraSettings,
    Settings,
    _https_url,
    _require,
    load_productos_settings,
    load_settings,
)
from agent_host.identity import EntraIdentity, IdentityError, load_certificate_pem
from agent_host.productos import ProductAnswer, ask, build_productos_agent
from agent_host.workflow import Conversation, build_triage_agent, build_workflow, escalate_only_tool

TOOL_LABELS = {
    "block_card": "Bloquear tarjeta",
    "open_claim": "Abrir reclamo",
}
AGENT_LABELS = {"triage": "Triage", "productos": "Productos", "operaciones": "Operaciones"}


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


def reply_text(agent: str, text: str) -> str:
    """Productos answers with ProductAnswer JSON; the other agents with plain text."""
    if agent == "productos":
        with contextlib.suppress(ValueError):
            text = format_answer(ProductAnswer.model_validate_json(text))
    return text


def format_reply(agent: str, text: str) -> str:
    return f"[{AGENT_LABELS.get(agent, agent)}] {reply_text(agent, text)}"


def customer_identity(settings: Settings, credential) -> tuple[Identity, str]:
    """(identity for the MCP headers, label to show). AUTH_MODE=entra signs in by device code."""
    if settings.entra is None:
        return settings.dev_customer_id, f"Cliente {settings.dev_customer_id} (modo dev)"
    entra = settings.entra
    identity = EntraIdentity(entra, lambda: load_certificate_pem(entra, credential))
    name = identity.sign_in()
    identity.mcp_token()  # fail fast: OBO + Key Vault before the first question
    return identity.mcp_token, f"Sesión iniciada: {name}"


def _ignore_mcp_close_noise(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # ponytail: the workflow opens MCP connections inside its own tasks, so closing them from
    # the main task trips anyio's cancel-scope check at shutdown. The process is exiting and
    # nothing is lost; drop only that message. Remove when MAF closes them in the same task.
    if "streamable_http_client" in repr(context.get("asyncgen")):
        return
    loop.default_exception_handler(context)


async def run_triage(args: argparse.Namespace) -> None:
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    settings = load_settings()
    kb_settings = load_productos_settings()
    identity, who = customer_identity(settings, SyncCredential())
    asyncio.get_running_loop().set_exception_handler(_ignore_mcp_close_noise)
    async with DefaultAzureCredential() as credential:
        client = build_foundry_client(settings, credential)
        agents = [
            build_triage_agent(client, settings),
            build_productos_agent(
                client, kb_settings, SyncCredential(), extra_tools=[escalate_only_tool(settings)]
            ),
            build_agent(client, settings),
        ]
        try:
            conversation = Conversation(build_workflow(*agents), identity)
            print(f"{who} | conversación {conversation.conversation_id}")

            async def turn(text: str, decide) -> None:
                result = await conversation.send(text, decide)
                for source, target in result.handoffs:
                    print(f"  -> {AGENT_LABELS[source]} transfiere a {AGENT_LABELS[target]}")
                for agent, reply in result.replies:
                    print(f"Agente {format_reply(agent, reply)}")

            if args.ask:
                decide = fixed_decide(args.auto_approve)  # deny unless --auto-approve
                for question in args.ask:
                    print(f"\nTú: {question}")
                    await turn(question, decide)
                return
            print("Escribe tu consulta. Ctrl+C o 'salir' para terminar.")
            while True:
                text = (await asyncio.to_thread(input, "\nTú: ")).strip()
                if text.lower() in {"salir", "exit"}:
                    return
                if text:
                    await turn(text, interactive_decide)
        finally:
            for agent in agents:
                await agent.close()


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
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.identity.aio import DefaultAzureCredential

    if args.agent == "productos":
        await run_productos(args)
        return
    if args.agent == "triage":
        await run_triage(args)
        return

    settings = load_settings()
    identity, who = customer_identity(settings, SyncCredential())
    async with DefaultAzureCredential() as credential:
        # No `async with agent`: that connects MCP eagerly, before a run supplies the identity
        # headers. The first run connects lazily with them; close() releases the connection.
        agent = build_agent(build_foundry_client(settings, credential), settings)
        try:
            session = new_session(agent, identity)
            print(f"{who} | conversación {session.session_id}")
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


def remote_auth() -> tuple[EntraIdentity | None, str]:
    """(identity or None in dev mode, label to show). Needs only the CLI settings."""
    env = os.environ
    if (env.get("AUTH_MODE", "").strip() or "dev") != "entra":
        return None, "modo dev (el host usa su DEV_CUSTOMER_ID)"
    _require(env, ("ENTRA_AUTHORITY", "ENTRA_CLI_CLIENT_ID", "ENTRA_AGENT_API_CLIENT_ID"))
    entra = EntraSettings(
        authority=_https_url("ENTRA_AUTHORITY", env["ENTRA_AUTHORITY"]).rstrip("/"),
        cli_client_id=env["ENTRA_CLI_CLIENT_ID"].strip(),
        agent_api_client_id=env["ENTRA_AGENT_API_CLIENT_ID"].strip(),
        mcp_api_client_id="",  # the remote host does the OBO, not the CLI
        key_vault_uri="",
        cert_name="",
    )
    identity = EntraIdentity(entra, load_pem=lambda: "")
    return identity, f"Sesión iniciada: {identity.sign_in()}"


def describe_pending(pending: dict) -> str:
    label = TOOL_LABELS.get(pending["tool"], "Operación")
    shown = json.dumps(pending["args"], ensure_ascii=False) if pending["args"] else "sin argumentos"
    return f"{label} ({pending['tool']}) con {shown}"


def run_remote(args: argparse.Namespace) -> None:
    import httpx

    identity, who = remote_auth()
    url = _https_url("--remote", args.remote, allow_http_localhost=True).rstrip("/") + "/chat"
    conversation_id: str | None = None

    def post(body: dict) -> dict:
        nonlocal conversation_id
        headers = {"Authorization": f"Bearer {identity.user_token()}"} if identity else {}
        if conversation_id:
            body = {"conversation_id": conversation_id, **body}
        response = httpx.post(url, json=body, headers=headers, timeout=150)
        data = response.json()
        if response.status_code != 200:
            raise IdentityError(data.get("text") or data.get("error", "Error del agente."))
        conversation_id = data["conversation_id"]
        for reply in data["replies"]:
            print(f"Agente [{AGENT_LABELS.get(reply['agent'], reply['agent'])}] {reply['text']}")
        return data

    def turn(text: str, decide) -> None:
        data = post({"message": text})
        while data["pending_approvals"]:
            approvals = []
            for pending in data["pending_approvals"]:
                approved = decide(pending)
                approvals.append({"request_id": pending["request_id"], "approved": approved})
            data = post({"approvals": approvals})

    def ask_human(pending: dict) -> bool:
        print(f"\nEl agente quiere ejecutar: {describe_pending(pending)}")
        return parse_answer(input("¿Apruebas? (s/n): "))

    def fixed(pending: dict) -> bool:
        verdict = "aprobada" if args.auto_approve else "rechazada"
        print(f"[aprobación] {describe_pending(pending)} -> {verdict}")
        return args.auto_approve

    print(f"{who} | host remoto {args.remote}")
    if args.ask:
        for question in args.ask:
            print(f"\nTú: {question}")
            turn(question, fixed)
        return
    print("Escribe tu consulta. Ctrl+C o 'salir' para terminar.")
    while True:
        text = input("\nTú: ").strip()
        if text.lower() in {"salir", "exit"}:
            return
        if text:
            try:
                turn(text, ask_human)
            except IdentityError as exc:
                print(f"Agente: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_host", description="Agentes de Banco Andino Demo.")
    parser.add_argument(
        "--agent",
        choices=["triage", "operaciones", "productos"],
        default="triage",
        help="triage (defecto) orquesta; operaciones y productos, para probarlos solos.",
    )
    parser.add_argument("--ask", action="append", help="Pregunta no interactiva (repetible).")
    parser.add_argument("--remote", help="URL del host HTTP (Fase 6); por defecto, en proceso.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--auto-approve", action="store_true", help="Aprueba toda escritura.")
    mode.add_argument("--auto-deny", action="store_true", help="Rechaza toda escritura (defecto).")
    args = parser.parse_args(argv)
    # MAF 1.24 still sends MCP pings, deprecated in mcp 2.3; harmless, but noisy in a demo.
    warnings.filterwarnings("ignore", message="ping is removed")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    try:
        if args.remote:
            run_remote(args)
        else:
            asyncio.run(run(args))
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    except IdentityError as exc:
        print(f"Identidad: {exc}", file=sys.stderr)
        return 3
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
    return 0
