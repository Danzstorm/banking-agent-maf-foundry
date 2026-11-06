from datetime import date

import pytest
from sqlalchemy import func, select

from mcp_server import banking
from mcp_server.banking import BankError
from mcp_server.db import cards, claims
from tests.conftest import CARD_A, CARD_B, CLAIM_A, CLAIM_B, A, B


def count_claims(engine, customer_id):
    with engine.connect() as conn:
        return conn.scalar(
            select(func.count()).select_from(claims).where(claims.c.cliente_id == customer_id)
        )


def card_state(engine, card_id):
    with engine.connect() as conn:
        return conn.scalar(select(cards.c.estado).where(cards.c.id == card_id))


def test_get_balance_returns_only_own_accounts_and_cards(engine):
    assert banking.get_balance(engine, A) == {
        "cuentas": [
            {
                "numero": "0001",
                "tipo": "cuenta_corriente",
                "saldo": "1000.50",
                "moneda": "CLP",
                "tarjetas": [{"card_id": CARD_A, "ultimos4": "1111", "estado": "activa"}],
            }
        ]
    }


def test_list_transactions_filters_by_inclusive_range_and_customer(engine):
    result = banking.list_transactions(engine, A, date(2026, 10, 15), date(2026, 10, 31))
    assert [m["descripcion"] for m in result["movimientos"]] == ["Mov 31", "Mov 15"]
    assert {m["cuenta"] for m in result["movimientos"]} == {"0001"}


@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [(date(2026, 10, 31), date(2026, 10, 1)), (date(2026, 1, 1), date(2026, 10, 1))],
)
def test_list_transactions_rejects_invalid_ranges(engine, date_from, date_to):
    with pytest.raises(BankError):
        banking.list_transactions(engine, A, date_from, date_to)


def test_get_claim_status_own_claim(engine):
    assert banking.get_claim_status(engine, A, CLAIM_A)["estado"] == "abierto"


@pytest.mark.parametrize("claim_id", [CLAIM_B, 999])
def test_get_claim_status_foreign_or_missing_gives_same_not_found(engine, claim_id):
    with pytest.raises(BankError, match=r"^Reclamo no encontrado\.$"):
        banking.get_claim_status(engine, A, claim_id)


def test_block_card_blocks_own_card(engine):
    assert banking.block_card(engine, A, "conv-1", CARD_A)["estado"] == "bloqueada"
    assert card_state(engine, CARD_A) == "bloqueada"


@pytest.mark.parametrize("card_id", [CARD_B, 999])
def test_block_card_cannot_touch_foreign_or_missing_card(engine, card_id):
    with pytest.raises(BankError, match=r"^Tarjeta no encontrada\.$"):
        banking.block_card(engine, A, "conv-1", card_id)
    assert card_state(engine, CARD_B) == "activa"


def test_block_card_twice_returns_same_result(engine):
    first = banking.block_card(engine, A, "conv-1", CARD_A)
    assert banking.block_card(engine, A, "conv-1", CARD_A) == first  # stored result
    assert banking.block_card(engine, A, "conv-2", CARD_A)["estado"] == "bloqueada"  # natural
    assert card_state(engine, CARD_A) == "bloqueada"


def test_open_claim_same_conversation_creates_one_claim(engine):
    first = banking.open_claim(engine, A, "conv-1", "Cobro duplicado en mi tarjeta")
    again = banking.open_claim(engine, A, "conv-1", "  cobro duplicado en   mi tarjeta ")
    assert again == first
    assert count_claims(engine, A) == 2  # fixture claim + 1


def test_open_claim_different_conversation_creates_two_claims(engine):
    first = banking.open_claim(engine, A, "conv-1", "Cobro duplicado en mi tarjeta")
    second = banking.open_claim(engine, A, "conv-2", "Cobro duplicado en mi tarjeta")
    assert first["claim_id"] != second["claim_id"]
    assert count_claims(engine, A) == 3


def test_same_conversation_id_never_shares_results_between_customers(engine):
    a = banking.open_claim(engine, A, "conv-1", "Cobro duplicado en mi tarjeta")
    b = banking.open_claim(engine, B, "conv-1", "Cobro duplicado en mi tarjeta")
    assert a["claim_id"] != b["claim_id"]


@pytest.mark.parametrize("motivo", ["corto", "x" * 501, "   a         b   "])
def test_open_claim_validates_motivo_length(engine, motivo):
    with pytest.raises(BankError):
        banking.open_claim(engine, A, "conv-1", motivo)
    assert count_claims(engine, A) == 1


def test_failed_write_is_not_cached(engine):
    for _ in range(2):  # the retry re-executes and fails again, no stored success
        with pytest.raises(BankError):
            banking.block_card(engine, A, "conv-1", CARD_B)
