"""Schema of the simulated core banking (see docs/ARCHITECTURE.md, section 7).

Table and column names are Spanish (domain language); code identifiers are English.
SQLAlchemy Core keeps the same schema portable to Azure SQL (only DATABASE_URL changes):
Unicode/UnicodeText map to NVARCHAR on SQL Server so accented names survive any collation.

Azure SQL (driver mssql-python, Entra auth, no passwords):
  container: mssql+mssqlpython://@<server>.database.windows.net:1433/<db>
             ?Authentication=ActiveDirectoryMSI&UID=<identity-client-id>&Encrypt=yes
  local:     same URL with Authentication=ActiveDirectoryDefault (uses your `az login`).
"""

import os
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Unicode,
    UnicodeText,
    create_engine,
    event,
)

DEFAULT_DATABASE_URL = "sqlite:///./data/banco.db"

metadata = MetaData()

customers = Table(
    "clientes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("entra_oid", Unicode(64), unique=True, nullable=False),
    Column("nombre", Unicode(120), nullable=False),
    Column("email", Unicode(200), nullable=False),
    Column("creado_en", DateTime, nullable=False),
)

accounts = Table(
    "cuentas",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", ForeignKey("clientes.id"), nullable=False, index=True),
    Column("numero", Unicode(32), unique=True, nullable=False),
    Column("tipo", Unicode(30), nullable=False),
    Column("saldo", Numeric(14, 2), nullable=False),
    Column("moneda", Unicode(3), nullable=False),
)

cards = Table(
    "tarjetas",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", ForeignKey("clientes.id"), nullable=False, index=True),
    Column("cuenta_id", ForeignKey("cuentas.id"), nullable=False),
    Column("ultimos4", Unicode(4), nullable=False),
    Column("estado", Unicode(20), nullable=False),
    Column("bloqueada_en", DateTime),
)

transactions = Table(
    "movimientos",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cuenta_id", ForeignKey("cuentas.id"), nullable=False, index=True),
    Column("fecha", DateTime, nullable=False, index=True),
    Column("descripcion", Unicode(200), nullable=False),
    Column("monto", Numeric(14, 2), nullable=False),
)

claims = Table(
    "reclamos",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", ForeignKey("clientes.id"), nullable=False, index=True),
    Column("motivo", Unicode(500), nullable=False),
    Column("estado", Unicode(20), nullable=False),
    Column("creado_en", DateTime, nullable=False),
)

# The primary key is the UNIQUE constraint that makes retried writes run once (ADR-007).
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("idem_key", Unicode(64), primary_key=True),
    Column("tool", Unicode(50), nullable=False),
    Column("resultado_json", UnicodeText, nullable=False),
    Column("creado_en", DateTime, nullable=False),
)

# Used from Phase 6 (durable sessions, ADR-013); created now so the schema matches the docs.
sessions = Table(
    "sesiones",
    metadata,
    Column("conversation_id", Unicode(100), primary_key=True),
    Column("cliente_id", ForeignKey("clientes.id"), nullable=False),
    Column("estado_json", UnicodeText, nullable=False),
    Column("actualizado_en", DateTime, nullable=False),
)

audit_log = Table(
    "auditoria",
    metadata,
    Column("id", Integer, primary_key=True),
    # Nullable: calls rejected before identity is resolved are audited too.
    Column("cliente_id", ForeignKey("clientes.id"), nullable=True),
    Column("tool", Unicode(50), nullable=False),
    Column("args_json", UnicodeText, nullable=False),
    Column("resultado", Unicode(20), nullable=False),
    Column("conversation_id", Unicode(100)),
    Column("trace_id", Unicode(32)),
    Column("creado_en", DateTime, nullable=False),
)

# Used from Phase 4 (escalate_to_human).
escalation_tickets = Table(
    "tickets_escalamiento",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("cliente_id", ForeignKey("clientes.id"), nullable=False),
    Column("conversation_id", Unicode(100)),
    Column("motivo", Unicode(500), nullable=False),
    Column("estado", Unicode(20), nullable=False),
    Column("creado_en", DateTime, nullable=False),
)


def make_engine(url: str | None = None) -> Engine:
    """Engine for DATABASE_URL; creates the SQLite folder and enforces foreign keys."""
    url = url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    # Pre-ping: Azure SQL serverless auto-pauses and drops idle pooled connections.
    engine = create_engine(url, pool_pre_ping=not url.startswith("sqlite"))
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    return engine
