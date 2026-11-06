"""Core banking operations behind the MCP tools.

Every function receives the customer id resolved from the request identity, never from
the LLM. Resources owned by another customer are reported exactly like missing ones,
so the caller cannot learn that they exist.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from mcp_server.db import (
    accounts,
    cards,
    claims,
    escalation_tickets,
    idempotency_keys,
    transactions,
)

MAX_RANGE_DAYS = 90
CLAIM_MIN_LEN = 10
CLAIM_MAX_LEN = 500
ESCALATION_CATEGORIES = ("fraude", "fallecimiento", "legal", "sin_fuente", "otro")
ESCALATION_MAX_LEN = 480  # leaves room for the "[categoria] " prefix in a 500-char column


class BankError(Exception):
    """Anticipated failure; its message is safe to show to the model and the customer."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _money(value) -> str:
    return f"{value:.2f}"


def get_balance(engine: Engine, customer_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        account_rows = conn.execute(
            select(accounts).where(accounts.c.cliente_id == customer_id).order_by(accounts.c.id)
        ).all()
        card_rows = conn.execute(
            select(cards).where(cards.c.cliente_id == customer_id).order_by(cards.c.id)
        ).all()
    return {
        "cuentas": [
            {
                "numero": a.numero,
                "tipo": a.tipo,
                "saldo": _money(a.saldo),
                "moneda": a.moneda,
                "tarjetas": [
                    {"card_id": c.id, "ultimos4": c.ultimos4, "estado": c.estado}
                    for c in card_rows
                    if c.cuenta_id == a.id
                ],
            }
            for a in account_rows
        ]
    }


def list_transactions(
    engine: Engine, customer_id: int, date_from: date, date_to: date
) -> dict[str, Any]:
    if date_from > date_to:
        raise BankError("date_from debe ser anterior o igual a date_to.")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise BankError(f"El rango máximo es de {MAX_RANGE_DAYS} días.")
    start = datetime.combine(date_from, time.min)
    end = datetime.combine(date_to + timedelta(days=1), time.min)
    query = (
        select(transactions, accounts.c.numero)
        .join(accounts, transactions.c.cuenta_id == accounts.c.id)
        .where(
            accounts.c.cliente_id == customer_id,
            transactions.c.fecha >= start,
            transactions.c.fecha < end,
        )
        .order_by(transactions.c.fecha.desc(), transactions.c.id.desc())
    )
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return {
        "desde": date_from.isoformat(),
        "hasta": date_to.isoformat(),
        "movimientos": [
            {
                "fecha": r.fecha.isoformat(),
                "cuenta": r.numero,
                "descripcion": r.descripcion,
                "monto": _money(r.monto),
            }
            for r in rows
        ],
    }


def get_claim_status(engine: Engine, customer_id: int, claim_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            select(claims).where(claims.c.id == claim_id, claims.c.cliente_id == customer_id)
        ).first()
    if row is None:
        raise BankError("Reclamo no encontrado.")
    return {
        "claim_id": row.id,
        "motivo": row.motivo,
        "estado": row.estado,
        "creado_en": row.creado_en.isoformat(),
    }


def idempotency_key(conversation_id: str, customer_id: int, tool: str, args: dict) -> str:
    # customer_id is part of the key so two customers can never share a stored result.
    material = {"conversation_id": conversation_id, "customer_id": customer_id}
    material |= {"tool": tool, "args": args}
    material = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode()).hexdigest()


def _run_once(
    engine: Engine, key: str, tool: str, work: Callable[[Connection], dict[str, Any]]
) -> dict[str, Any]:
    """Execute `work` once per key. The key insert and the write share one transaction, so a
    failed write stores nothing and a concurrent duplicate hits the UNIQUE constraint."""
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(idempotency_keys).values(
                    idem_key=key, tool=tool, resultado_json="", creado_en=_now()
                )
            )
            result = work(conn)
            conn.execute(
                update(idempotency_keys)
                .where(idempotency_keys.c.idem_key == key)
                .values(resultado_json=json.dumps(result, ensure_ascii=False))
            )
            return result
    except IntegrityError:
        with engine.connect() as conn:
            stored = conn.execute(
                select(idempotency_keys.c.resultado_json).where(idempotency_keys.c.idem_key == key)
            ).scalar_one_or_none()
        if not stored:
            raise
        return json.loads(stored)


def block_card(engine: Engine, customer_id: int, conversation_id: str, card_id: int):
    def work(conn: Connection) -> dict[str, Any]:
        card = conn.execute(
            select(cards).where(cards.c.id == card_id, cards.c.cliente_id == customer_id)
        ).first()
        if card is None:
            raise BankError("Tarjeta no encontrada.")
        if card.estado != "bloqueada":  # already blocked: same success, no second write
            conn.execute(
                update(cards)
                .where(cards.c.id == card_id)
                .values(estado="bloqueada", bloqueada_en=_now())
            )
        return {
            "card_id": card.id,
            "ultimos4": card.ultimos4,
            "estado": "bloqueada",
            "mensaje": f"La tarjeta terminada en {card.ultimos4} está bloqueada.",
        }

    key = idempotency_key(conversation_id, customer_id, "block_card", {"card_id": card_id})
    return _run_once(engine, key, "block_card", work)


def open_claim(engine: Engine, customer_id: int, conversation_id: str, motivo: str):
    motivo = " ".join(motivo.split())
    if not CLAIM_MIN_LEN <= len(motivo) <= CLAIM_MAX_LEN:
        raise BankError(f"El motivo debe tener entre {CLAIM_MIN_LEN} y {CLAIM_MAX_LEN} caracteres.")

    def work(conn: Connection) -> dict[str, Any]:
        created = _now()
        claim_id = conn.execute(
            insert(claims).values(
                cliente_id=customer_id, motivo=motivo, estado="abierto", creado_en=created
            )
        ).inserted_primary_key[0]
        return {
            "claim_id": claim_id,
            "estado": "abierto",
            "creado_en": created.isoformat(),
            "mensaje": f"Reclamo {claim_id} abierto.",
        }

    key = idempotency_key(conversation_id, customer_id, "open_claim", {"motivo": motivo.casefold()})
    return _run_once(engine, key, "open_claim", work)


def escalate_to_human(
    engine: Engine, customer_id: int, conversation_id: str, categoria: str, motivo: str
) -> dict[str, Any]:
    """Opens a ticket for a human advisor. One ticket per conversation and category."""
    motivo = " ".join(motivo.split())
    if categoria not in ESCALATION_CATEGORIES:
        raise BankError(f"Categoría inválida. Usa una de: {', '.join(ESCALATION_CATEGORIES)}.")
    if not CLAIM_MIN_LEN <= len(motivo) <= ESCALATION_MAX_LEN:
        raise BankError(
            f"El motivo debe tener entre {CLAIM_MIN_LEN} y {ESCALATION_MAX_LEN} caracteres."
        )

    def work(conn: Connection) -> dict[str, Any]:
        created = _now()
        ticket_id = conn.execute(
            insert(escalation_tickets).values(
                cliente_id=customer_id,
                conversation_id=conversation_id,
                # ponytail: category lives in the motivo text; add a column (and re-seed) if
                # advisors need to filter tickets by category.
                motivo=f"[{categoria}] {motivo}",
                estado="abierto",
                creado_en=created,
            )
        ).inserted_primary_key[0]
        return {
            "ticket_id": ticket_id,
            "categoria": categoria,
            "estado": "abierto",
            "mensaje": f"Ticket {ticket_id} creado: un asesor de Banco Andino Demo te contactará.",
        }

    # Keyed on the category only: a retry, or a second agent escalating the same topic in the
    # same conversation, returns the existing ticket instead of opening another.
    key = idempotency_key(
        conversation_id, customer_id, "escalate_to_human", {"categoria": categoria}
    )
    return _run_once(engine, key, "escalate_to_human", work)
