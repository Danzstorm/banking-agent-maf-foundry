from sqlalchemy import select

from mcp_server.db import make_engine, metadata
from mcp_server.seed import seed


def dump(engine):
    with engine.connect() as conn:
        return {
            t.name: conn.execute(select(t).order_by(*t.primary_key)).all()
            for t in metadata.sorted_tables
        }


def test_seed_is_reproducible(tmp_path):
    first, second = (make_engine(f"sqlite:///{tmp_path / n}") for n in ("a.db", "b.db"))
    seed(first)
    seed(second)
    data = dump(first)
    assert data == dump(second)
    assert len(data["clientes"]) == 10
    assert len(data["movimientos"]) > 100
    assert data["reclamos"]
    assert all(row.saldo >= 0 for row in data["cuentas"])
