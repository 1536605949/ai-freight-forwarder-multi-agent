"""Durable LangGraph checkpoints, stored in SQL.

Why a checkpointer, when `pipeline_graph_runs` already stores a cursor
-----------------------------------------------------------------------
The cursor answers "where did the run stop". It cannot answer "what did the state look like
at that point", because it is a summary written by the runner *after* the graph returned.
A checkpointer writes a snapshot after every node, which is what makes a trace replayable and
an incident reviewable without re-running the workflow against live data -- and what makes the
`GET /api/v1/graph/runs/{run_id}/checkpoints` endpoint possible at all.

The two stores do not compete. `pipeline_graph_runs` is the business-facing record (status,
pending gate, resume node). This table is the execution record (channel values per step). A
crash between them loses the summary but keeps the snapshots, which is the right way round:
the snapshots are the evidence.

Why not `MemorySaver`
---------------------
A quote can wait hours for a reviewer, and a deploy happens in between. The argument that
ruled out an in-memory checkpointer for the cursor applies identically here.

Why the async methods do not delegate to the sync ones
------------------------------------------------------
`BaseCheckpointSaver.aput` / `aget_tuple` raise `NotImplementedError` by default, and the
obvious fix -- `asyncio.to_thread(self.put, ...)` -- would move a SQLAlchemy `Session` onto
another thread, which SQLAlchemy does not support. Every async method here does the work
inline on the event-loop thread with the session that built the graph.

Blobs are stored base64-encoded `Text` rather than `LargeBinary`: the same schema then works
on SQLite and PostgreSQL without a driver-specific byte type, and a checkpoint is small
(one node's channel values).
"""
from __future__ import annotations

import base64
from typing import Any, Iterator, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.orm import Session

from app.models import GraphCheckpoint, GraphCheckpointWrite

_DEFAULT_NS = ''


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode('ascii'))


class SqlCheckpointSaver(BaseCheckpointSaver):
    """A `BaseCheckpointSaver` over the `graph_checkpoints` tables.

    One saver per request, holding the session that built the graph. It is not thread-safe
    and does not pretend to be: the graph runs on the caller's event loop, and the session is
    the caller's session.
    """

    def __init__(self, db: Session, *, tenant_id: str | None = None,
                 run_id: str | None = None, serde=None) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self.db = db
        self.tenant_id = tenant_id
        self.run_id = run_id

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _ids(config: dict) -> tuple[str, str]:
        configurable = (config or {}).get('configurable') or {}
        thread_id = configurable.get('thread_id')
        if not thread_id:
            raise ValueError('checkpointer_requires_thread_id')
        return thread_id, configurable.get('checkpoint_ns') or _DEFAULT_NS

    def _writes(self, thread_id: str, ns: str, checkpoint_id: str) -> list[tuple[str, str, Any]]:
        """`(task_id, channel, value)` triples, in `(task_id, idx)` order.

        The three-element shape is not cosmetic: `CheckpointTuple.pending_writes` is consumed
        by `read_task_statuses`, which unpacks exactly three values. Returning the natural
        `(channel, value)` pair type-checks as a list of tuples and then fails at replay time
        with `not enough values to unpack`.
        """
        rows = (self.db.query(GraphCheckpointWrite)
                .filter_by(thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id)
                .order_by(GraphCheckpointWrite.task_id, GraphCheckpointWrite.idx).all())
        return [(row.task_id, row.channel,
                 self.serde.loads_typed((row.value_type, _unb64(row.value_blob))))
                for row in rows]

    def _to_tuple(self, row: GraphCheckpoint) -> CheckpointTuple:
        checkpoint = self.serde.loads_typed(
            (row.checkpoint_type, _unb64(row.checkpoint_blob)))
        metadata = self.serde.loads_typed((row.metadata_type, _unb64(row.metadata_blob)))
        config = {'configurable': {
            'thread_id': row.thread_id,
            'checkpoint_ns': row.checkpoint_ns,
            'checkpoint_id': row.checkpoint_id,
        }}
        parent_config = None
        if row.parent_checkpoint_id:
            parent_config = {'configurable': {
                'thread_id': row.thread_id,
                'checkpoint_ns': row.checkpoint_ns,
                'checkpoint_id': row.parent_checkpoint_id,
            }}
        return CheckpointTuple(
            config=config, checkpoint=checkpoint, metadata=metadata,
            parent_config=parent_config,
            pending_writes=self._writes(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
        )

    # -- sync protocol ---------------------------------------------------- #

    def get_tuple(self, config: dict) -> CheckpointTuple | None:
        thread_id, ns = self._ids(config)
        checkpoint_id = ((config or {}).get('configurable') or {}).get('checkpoint_id')
        query = self.db.query(GraphCheckpoint).filter_by(thread_id=thread_id, checkpoint_ns=ns)
        if checkpoint_id:
            query = query.filter_by(checkpoint_id=checkpoint_id)
        # Checkpoint ids are time-ordered UUIDs, so the string order is the execution order.
        row = query.order_by(GraphCheckpoint.checkpoint_id.desc()).first()
        return self._to_tuple(row) if row else None

    def list(self, config: dict | None, *, filter: dict | None = None,
             before: dict | None = None, limit: int | None = None) -> Iterator[CheckpointTuple]:
        query = self.db.query(GraphCheckpoint)
        if config:
            thread_id, ns = self._ids(config)
            query = query.filter_by(thread_id=thread_id, checkpoint_ns=ns)
        if before:
            before_id = ((before or {}).get('configurable') or {}).get('checkpoint_id')
            if before_id:
                query = query.filter(GraphCheckpoint.checkpoint_id < before_id)
        query = query.order_by(GraphCheckpoint.checkpoint_id.desc())
        if limit:
            query = query.limit(limit)
        for row in query.all():
            yield self._to_tuple(row)

    def put(self, config: dict, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> dict:
        thread_id, ns = self._ids(config)
        checkpoint_id = checkpoint['id']
        parent = ((config or {}).get('configurable') or {}).get('checkpoint_id')
        checkpoint_type, checkpoint_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(metadata)

        existing = self.db.query(GraphCheckpoint).filter_by(
            thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id).first()
        if existing is None:
            self.db.add(GraphCheckpoint(
                thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id,
                parent_checkpoint_id=parent, tenant_id=self.tenant_id, run_id=self.run_id,
                checkpoint_type=checkpoint_type, checkpoint_blob=_b64(checkpoint_bytes),
                metadata_type=metadata_type, metadata_blob=_b64(metadata_bytes),
            ))
        else:
            # LangGraph may re-write the same checkpoint id; the newer value wins, but the
            # parent pointer is kept so the history chain does not develop a hole.
            existing.checkpoint_type = checkpoint_type
            existing.checkpoint_blob = _b64(checkpoint_bytes)
            existing.metadata_type = metadata_type
            existing.metadata_blob = _b64(metadata_bytes)
        self.db.commit()
        return {'configurable': {'thread_id': thread_id, 'checkpoint_ns': ns,
                                 'checkpoint_id': checkpoint_id}}

    def put_writes(self, config: dict, writes: Sequence[tuple[str, Any]], task_id: str,
                   task_path: str = '') -> None:
        thread_id, ns = self._ids(config)
        checkpoint_id = ((config or {}).get('configurable') or {}).get('checkpoint_id')
        if not checkpoint_id:
            raise ValueError('put_writes_requires_checkpoint_id')
        for idx, (channel, value) in enumerate(writes):
            value_type, value_bytes = self.serde.dumps_typed(value)
            self.db.add(GraphCheckpointWrite(
                thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id,
                task_id=task_id, idx=idx, channel=channel,
                value_type=value_type, value_blob=_b64(value_bytes),
            ))
        self.db.commit()

    def delete_thread(self, thread_id: str) -> None:
        self.db.query(GraphCheckpointWrite).filter_by(thread_id=thread_id).delete()
        self.db.query(GraphCheckpoint).filter_by(thread_id=thread_id).delete()
        self.db.commit()

    # -- async protocol --------------------------------------------------- #
    # Inline, not `to_thread`: see the module docstring.

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(self, config: dict | None, *, filter: dict | None = None,
                    before: dict | None = None, limit: int | None = None):
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(self, config: dict, checkpoint: Checkpoint, metadata: CheckpointMetadata,
                   new_versions: ChannelVersions) -> dict:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: dict, writes: Sequence[tuple[str, Any]], task_id: str,
                          task_path: str = '') -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)


__all__ = ['SqlCheckpointSaver', 'read_history']


def read_history(db: Session, thread_id: str, *, limit: int = 200) -> list[dict]:
    """Decode one thread's checkpoints into a timeline, oldest first.

    A summary rather than the raw channel values: an operator reviewing an incident needs to
    see *which node ran and what the run was waiting for*, not a dump of every channel. The
    raw blob stays in the row for a replay that needs it.
    """
    serde = JsonPlusSerializer()
    rows = (db.query(GraphCheckpoint)
            .filter_by(thread_id=thread_id)
            .order_by(GraphCheckpoint.checkpoint_id).limit(limit).all())
    out: list[dict] = []
    for row in rows:
        checkpoint = serde.loads_typed((row.checkpoint_type, _unb64(row.checkpoint_blob)))
        metadata = serde.loads_typed((row.metadata_type, _unb64(row.metadata_blob)))
        values = checkpoint.get('channel_values') or {}
        steps = values.get('steps') or []
        out.append({
            'checkpoint_id': row.checkpoint_id,
            'parent_checkpoint_id': row.parent_checkpoint_id,
            'step': metadata.get('step'),
            'source': metadata.get('source'),
            'status': values.get('status'),
            'stop_reason': values.get('stop_reason'),
            'nodes': [s.get('node') for s in steps],
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })
    return out
