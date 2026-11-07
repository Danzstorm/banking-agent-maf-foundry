"""Durable conversations (ADR-013): the workflow checkpoint lives in `sesiones`, one row each.

`HandoffBuilder` workflows checkpoint at every superstep boundary, including the pause where a
write waits for approval (`request_info`). `SqlCheckpointStorage` keeps only the latest
checkpoint of one conversation in `sesiones.estado_json`, so any replica can rebuild the
workflow, restore it with `workflow.run(checkpoint_id=..., responses=...)` and continue.

The row also stores the owner (`cliente_id`): a conversation is only resumed for the customer
that started it. Encoding is MAF's own checkpoint encoding (JSON with restricted pickle for
framework types), decoded with the same allowlist on load.
"""

import json
from datetime import UTC, datetime
from typing import Any

from agent_framework import WorkflowCheckpoint
from agent_framework._workflows._checkpoint import _encode_checkpoint_for_storage
from agent_framework._workflows._checkpoint_encoding import decode_checkpoint_value
from agent_framework.exceptions import WorkflowCheckpointException
from sqlalchemy import Engine, create_engine, insert, select, update

from agent_host.resilience import db_retry
from mcp_server.db import customers, make_engine, sessions

# HandoffAgentUserRequest carries `list[Message]` as its response type: a types.GenericAlias.
# Without it the pause checkpoint fails to encode and MAF only logs the error.
ALLOWED_TYPES = frozenset({"types:GenericAlias"})


def sessions_engine(url: str) -> Engine:
    """SQLite (dev/tests): the full schema is created. Azure SQL: the seed already created it,
    and the host identity has no DDL rights; connecting lazily also lets the DB stay paused."""
    if url.startswith("sqlite"):
        return make_engine(url)
    return create_engine(url, pool_pre_ping=True)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def owner(engine: Engine, conversation_id: str) -> int | None:
    """Customer that owns the conversation, or None if it does not exist."""
    for attempt in db_retry():
        with attempt, engine.connect() as conn:
            return conn.scalar(
                select(sessions.c.cliente_id).where(sessions.c.conversation_id == conversation_id)
            )
    return None  # unreachable: db_retry re-raises


def customer_by_oid(engine: Engine, oid: str) -> int | None:
    for attempt in db_retry():
        with attempt, engine.connect() as conn:
            return conn.scalar(select(customers.c.id).where(customers.c.entra_oid == oid))
    return None


class SqlCheckpointStorage:
    """CheckpointStorage for ONE conversation: keeps its latest checkpoint.

    ponytail: one row, last write wins. Two concurrent turns on the same conversation (two
    browser tabs) can overwrite each other; add optimistic concurrency on `actualizado_en`
    if that becomes real.
    """

    def __init__(self, engine: Engine, conversation_id: str, customer_id: int) -> None:
        self.engine = engine
        self.conversation_id = conversation_id
        self.customer_id = customer_id
        self.saved: WorkflowCheckpoint | None = None  # last checkpoint written by this instance

    def _read(self) -> str | None:
        for attempt in db_retry():
            with attempt, self.engine.connect() as conn:
                return conn.scalar(
                    select(sessions.c.estado_json).where(
                        sessions.c.conversation_id == self.conversation_id,
                        sessions.c.cliente_id == self.customer_id,
                    )
                )
        return None

    def _write(self, blob: str) -> None:
        values = {"estado_json": blob, "actualizado_en": _now()}
        where = (sessions.c.conversation_id == self.conversation_id) & (
            sessions.c.cliente_id == self.customer_id
        )
        for attempt in db_retry():
            with attempt, self.engine.begin() as conn:
                if conn.execute(update(sessions).where(where).values(**values)).rowcount == 0:
                    conn.execute(
                        insert(sessions).values(
                            conversation_id=self.conversation_id,
                            cliente_id=self.customer_id,
                            **values,
                        )
                    )

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        encoded = _encode_checkpoint_for_storage(checkpoint, ALLOWED_TYPES)
        # ponytail: sync SQL on the event loop (a few ms per superstep); move to a thread or an
        # async driver if one replica serves many concurrent conversations.
        self._write(json.dumps(encoded, ensure_ascii=False))
        self.saved = checkpoint
        return checkpoint.checkpoint_id

    async def get_latest(self, *, workflow_name: str = "") -> WorkflowCheckpoint | None:
        blob = self._read()
        if blob is None:
            return None
        data = decode_checkpoint_value(json.loads(blob), allowed_types=ALLOWED_TYPES)
        return WorkflowCheckpoint.from_dict(data)

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        latest = await self.get_latest()
        if latest is None or latest.checkpoint_id != checkpoint_id:
            raise WorkflowCheckpointException(f"No checkpoint found with ID {checkpoint_id}")
        return latest

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        latest = await self.get_latest()
        return [latest] if latest else []

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        return [c.checkpoint_id for c in await self.list_checkpoints(workflow_name=workflow_name)]

    async def delete(self, checkpoint_id: str) -> bool:
        return False  # the row is the conversation; it is overwritten, never deleted here


def pending(checkpoint: WorkflowCheckpoint | None) -> dict[str, Any]:
    """request_id -> request payload still waiting for an answer in the checkpoint."""
    if checkpoint is None:
        return {}
    return {rid: event.data for rid, event in checkpoint.pending_request_info_events.items()}
