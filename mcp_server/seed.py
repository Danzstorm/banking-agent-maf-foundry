"""Reproducible synthetic data for Banco Andino Demo: `uv run python -m mcp_server.seed`.

Fixed seed and fixed reference date: every run produces exactly the same rows.
The database is recreated from scratch (drop + create).
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import Engine, insert

from mcp_server.db import accounts, cards, claims, customers, make_engine, metadata, transactions

SEED = 20261106
REFERENCE_DATE = datetime(2026, 11, 1)  # last day with movements
DAYS = 60
N_CUSTOMERS = 10
CURRENCY = "PEN"

EXPENSES = [
    "Supermercado",
    "Farmacia",
    "Combustible",
    "Restaurante",
    "Pago de servicios",
    "Transporte",
    "Tienda en línea",
    "Cine",
]
INCOMES = ["Transferencia recibida", "Depósito", "Abono de remuneraciones"]
CLAIM_REASONS = [
    ("Cobro duplicado en compra de supermercado", "en_revision"),
    ("No reconozco un cargo en tienda en línea", "abierto"),
    ("La transferencia recibida no aparece en mi cuenta", "resuelto"),
    ("Cobro de comisión que no corresponde", "abierto"),
    ("El cajero no entregó el dinero pero sí lo descontó", "en_revision"),
]


def seed(engine: Engine) -> None:
    fake = Faker("es")
    fake.seed_instance(SEED)
    rnd = fake.random
    start = REFERENCE_DATE - timedelta(days=DAYS - 1)

    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        for cid in range(1, N_CUSTOMERS + 1):
            conn.execute(
                insert(customers).values(
                    id=cid,
                    entra_oid=str(uuid.UUID(int=rnd.getrandbits(128), version=4)),
                    nombre=fake.name(),
                    email=f"cliente{cid}@ejemplo.test",
                    creado_en=start - timedelta(days=rnd.randint(100, 900)),
                )
            )
            kinds = ["cuenta_corriente"] + (["cuenta_ahorro"] if rnd.random() < 0.5 else [])
            for kind in kinds:
                number = f"{rnd.randint(10**9, 10**10 - 1)}"
                balance = Decimal(rnd.randint(200_000, 3_000_000)) / 100  # S/ 2,000-30,000
                account_id = conn.execute(
                    insert(accounts).values(
                        cliente_id=cid,
                        numero=number,
                        tipo=kind,
                        saldo=0,
                        moneda=CURRENCY,
                    )
                ).inserted_primary_key[0]

                movements = []
                for day in range(DAYS):
                    for _ in range(rnd.randint(0, 2 if kind == "cuenta_corriente" else 1)):
                        income = rnd.random() < 0.2
                        amount = (
                            Decimal(
                                rnd.randint(5_000, 400_000)
                                if income
                                else -rnd.randint(1_000, 80_000)
                            )
                            / 100
                        )
                        if balance + amount < 0:  # synthetic accounts never overdraw
                            continue
                        label = rnd.choice(INCOMES if income else EXPENSES)
                        movements.append(
                            {
                                "cuenta_id": account_id,
                                "fecha": start
                                + timedelta(days=day, minutes=rnd.randint(480, 1320)),
                                "descripcion": f"{label} - {fake.company()}"[:200],
                                "monto": amount,
                            }
                        )
                        balance += amount
                if movements:
                    conn.execute(insert(transactions), movements)
                conn.execute(
                    accounts.update().where(accounts.c.id == account_id).values(saldo=balance)
                )

                if kind == "cuenta_corriente":
                    blocked = cid == N_CUSTOMERS  # one card already blocked, for demos
                    conn.execute(
                        insert(cards).values(
                            cliente_id=cid,
                            cuenta_id=account_id,
                            ultimos4=f"{rnd.randint(0, 9999):04d}",
                            estado="bloqueada" if blocked else "activa",
                            bloqueada_en=REFERENCE_DATE if blocked else None,
                        )
                    )

        for i, (reason, status) in enumerate(CLAIM_REASONS):
            conn.execute(
                insert(claims).values(
                    cliente_id=1 + (i * 2) % N_CUSTOMERS,
                    motivo=reason,
                    estado=status,
                    creado_en=REFERENCE_DATE - timedelta(days=rnd.randint(1, DAYS)),
                )
            )


if __name__ == "__main__":
    engine = make_engine()
    seed(engine)
    print(f"Seed listo: {N_CUSTOMERS} clientes en {engine.url}")
