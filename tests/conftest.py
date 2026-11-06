import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import insert

from mcp_server.db import accounts, cards, claims, customers, make_engine, transactions

ROOT = Path(__file__).resolve().parents[1]
A, B = 1, 2  # customer ids
CARD_A, CARD_B = 1, 2
CLAIM_A, CLAIM_B = 1, 2


def load_fixture(engine):
    """Two customers, each with one account, one card, one claim and three movements."""
    now = datetime(2026, 11, 1, 12, 0)
    with engine.begin() as conn:
        for cid in (A, B):
            conn.execute(
                insert(customers).values(
                    id=cid,
                    entra_oid=f"oid-{cid}",
                    nombre=f"Cliente {cid}",
                    email=f"c{cid}@ejemplo.test",
                    creado_en=now,
                )
            )
            conn.execute(
                insert(accounts).values(
                    id=cid,
                    cliente_id=cid,
                    numero=f"000{cid}",
                    tipo="cuenta_corriente",
                    saldo=Decimal("1000.50") * cid,
                    moneda="PEN",
                )
            )
            conn.execute(
                insert(cards).values(
                    id=cid, cliente_id=cid, cuenta_id=cid, ultimos4=f"111{cid}", estado="activa"
                )
            )
            conn.execute(
                insert(claims).values(
                    id=cid,
                    cliente_id=cid,
                    motivo=f"Reclamo del cliente {cid}",
                    estado="abierto",
                    creado_en=now,
                )
            )
            conn.execute(
                insert(transactions),
                [
                    {
                        "cuenta_id": cid,
                        "fecha": datetime(2026, 10, d, 10),
                        "descripcion": f"Mov {d}",
                        "monto": Decimal(-d),
                    }
                    for d in (1, 15, 31)
                ],
            )


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    load_fixture(engine)
    yield engine
    engine.dispose()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db_url = f"sqlite:///{tmp_path_factory.mktemp('contract') / 'contract.db'}"
    engine = make_engine(db_url)
    load_fixture(engine)
    port = _free_port()
    env = os.environ | {"DATABASE_URL": db_url, "AUTH_MODE": "dev", "MCP_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server"], cwd=ROOT, env=env)  # noqa: S603
    deadline = time.monotonic() + 20
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail("MCP server did not start")
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp", engine
    proc.terminate()
    proc.wait(timeout=10)
    engine.dispose()
