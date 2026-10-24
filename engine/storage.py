"""Conversation storage: sessions, messages, tool calls, latency, audit and guardrail events.

PostgreSQL in production (DATABASE_URL=postgresql+asyncpg://...); a local SQLite file when DATABASE_URL
is unset, and in tests. Writes go through an in-memory queue drained by a background worker, so a slow
database never adds latency to a reply. Retention keeps each customer's most recent conversations
(policy: retention.conversations_per_customer); audit and guardrail events are kept separately because
compliance records must outlive chat history.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, Text,
                        delete, event, insert, select, update)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .governance import policy

log = logging.getLogger("storage")
DEFAULT_SQLITE = Path(__file__).resolve().parent.parent / "data" / "local.db"

metadata = MetaData()

conversations = Table(
    "conversations", metadata,
    Column("conversation_id", String(32), primary_key=True),
    Column("customer_id", String(32), nullable=False, index=True),
    Column("channel", String(16)),
    Column("packs", JSON),
    Column("provider", String(32)),
    Column("model", String(64)),
    Column("title", String(200)),
    Column("turns", Integer, nullable=False, default=0),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("last_active_at", DateTime(timezone=True), nullable=False, index=True),
)


def _child(name: str, *cols: Column) -> Table:
    return Table(
        name, metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("conversation_id", String(32), ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
               nullable=False, index=True),
        Column("turn", Integer, nullable=False),
        *cols,
        Column("created_at", DateTime(timezone=True), nullable=False),
    )


messages = _child("messages", Column("role", String(16), nullable=False), Column("content", Text, nullable=False),
                  Column("channel", String(16)), Column("interrupted", Boolean, default=False),
                  Column("guardrails", JSON))
tool_calls = _child("tool_calls", Column("name", String(64), nullable=False), Column("args", JSON),
                    Column("ok", Boolean), Column("error", String(64)), Column("status", String(64)))
turn_metrics = _child("turn_metrics", Column("first_text_ms", Float), Column("total_ms", Float),
                      Column("llm_ms", Float), Column("tools_ms", Float), Column("steps", Integer),
                      Column("input_tokens", Integer), Column("output_tokens", Integer), Column("cached_tokens", Integer))

# Retained independently of chat history (no cascade from conversations).
audit_events = Table(
    "audit_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conversation_id", String(32), index=True),
    Column("customer_id", String(32), index=True),
    Column("turn", Integer),
    Column("event", String(64), nullable=False),
    Column("data", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
guardrail_events = Table(
    "guardrail_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conversation_id", String(32), index=True),
    Column("customer_id", String(32), index=True),
    Column("turn", Integer),
    Column("stage", String(16), nullable=False),
    Column("rule", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("detail", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
# Login sessions. Only a SHA-256 hash of the bearer token is stored; revoked rows are kept for audit.
auth_sessions = Table(
    "auth_sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("customer_id", String(32), nullable=False, index=True),
    Column("conversation_id", String(32), nullable=False),
    Column("channel", String(16)),
    Column("user_agent", String(200)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoked_reason", String(32)),
)
voice_metrics = Table(
    "voice_metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conversation_id", String(32), index=True),
    Column("customer_id", String(32)),
    Column("data", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

TABLES = {t.name: t for t in metadata.sorted_tables}


def now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL") or f"sqlite+aiosqlite:///{DEFAULT_SQLITE}"
        self.is_sqlite = self.url.startswith("sqlite")
        # No pre-ping: it costs a round trip per query, which matters when the database is far away.
        # Connections are recycled periodically instead, and a failed write is logged, never fatal.
        kwargs = {} if self.is_sqlite else {"pool_size": int(os.environ.get("DB_POOL_SIZE", 10)),
                                            "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", 20)),
                                            "pool_recycle": 1800}
        self.engine: AsyncEngine = create_async_engine(self.url, **kwargs)
        if self.is_sqlite:
            @event.listens_for(self.engine.sync_engine, "connect")
            def _fk_on(conn, _):  # SQLite enforces ON DELETE CASCADE only with this pragma
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=5000")  # wait for the writer instead of failing at once
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._enqueued = self._written = 0
        self._progress = asyncio.Condition()

    @property
    def backend(self) -> str:
        return "sqlite" if self.is_sqlite else self.engine.url.get_backend_name()

    async def init(self) -> None:
        if self.is_sqlite and ":memory:" not in self.url:
            DEFAULT_SQLITE.parent.mkdir(exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        self._worker = asyncio.create_task(self._drain())

    async def close(self) -> None:
        await self.flush()
        if self._worker:
            self._worker.cancel()
        await self.engine.dispose()

    # ---- write-behind queue ------------------------------------------------------------------------------

    def record(self, table: str, **row) -> None:
        """Queue an insert. Never blocks and never raises into the conversation."""
        row.setdefault("created_at", now())
        self._put(("insert", table, row))

    def touch(self, conversation_id: str, turns: int, title: str | None = None) -> None:
        values = {"turns": turns, "last_active_at": now()}
        if title:
            values["title"] = title[:200]
        self._put(("touch", conversation_id, values))

    def _put(self, item: tuple) -> None:
        self._queue.put_nowait(item)
        self._enqueued += 1

    async def flush(self) -> None:
        """Wait until everything queued before this call is written. Later writes don't extend the wait, so
        this stays bounded under sustained load (waiting for an empty queue might never finish)."""
        target = self._enqueued
        async with self._progress:
            await self._progress.wait_for(lambda: self._written >= target)

    async def _drain(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty() and len(batch) < 200:
                batch.append(self._queue.get_nowait())
            try:
                async with self.engine.begin() as conn:
                    for op, target, payload in batch:
                        if op == "insert":
                            await conn.execute(insert(TABLES[target]), [payload])
                        elif op == "retention":
                            await self._retain(conn, target)
                        elif op == "auth_touch":
                            await conn.execute(update(auth_sessions)
                                               .where(auth_sessions.c.token_hash == target).values(**payload))
                        else:
                            await conn.execute(update(conversations)
                                               .where(conversations.c.conversation_id == target).values(**payload))
            except Exception as e:  # storage problems must not break conversations
                log.warning("storage write failed (%d rows dropped): %s", len(batch), e)
            finally:
                async with self._progress:
                    self._written += len(batch)
                    self._progress.notify_all()

    # ---- conversations ------------------------------------------------------------------------------------

    async def start_conversation(self, conversation_id: str, customer_id: str, channel: str, packs: list[str],
                                 provider: str, model: str) -> None:
        """Queue the conversation row and a retention pass. The queue is FIFO, so the row lands before any of its
        messages; nothing here waits on the database, keeping sign-in fast even against a distant database."""
        ts = now()
        self._put(("insert", "conversations", dict(
            conversation_id=conversation_id, customer_id=customer_id, channel=channel, packs=packs,
            provider=provider, model=model, turns=0, started_at=ts, last_active_at=ts)))
        self._put(("retention", customer_id, None))

    async def apply_retention(self, customer_id: str) -> int:
        await self.flush()
        async with self.engine.begin() as conn:
            return await self._retain(conn, customer_id)

    async def _retain(self, conn, customer_id: str) -> int:
        keep = policy()["retention"]["conversations_per_customer"]
        rows = (await conn.execute(
            select(conversations.c.conversation_id).where(conversations.c.customer_id == customer_id)
            .order_by(conversations.c.last_active_at.desc(), conversations.c.started_at.desc()).offset(keep)
        )).scalars().all()
        if rows:
            await conn.execute(delete(conversations).where(conversations.c.conversation_id.in_(rows)))
        return len(rows)

    async def recent_conversations(self, customer_id: str) -> list[dict]:
        await self.flush()
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(conversations).where(conversations.c.customer_id == customer_id)
                .order_by(conversations.c.last_active_at.desc())
            )).mappings().all()
        return [{**r, "started_at": r["started_at"].isoformat(), "last_active_at": r["last_active_at"].isoformat()}
                for r in rows]

    async def transcript(self, conversation_id: str, customer_id: str) -> dict | None:
        """Messages and tool calls for one conversation, only if it belongs to this customer."""
        await self.flush()
        async with self.engine.connect() as conn:
            conv = (await conn.execute(select(conversations).where(
                conversations.c.conversation_id == conversation_id,
                conversations.c.customer_id == customer_id))).mappings().first()
            if not conv:
                return None
            msgs = (await conn.execute(select(messages).where(messages.c.conversation_id == conversation_id)
                                       .order_by(messages.c.id))).mappings().all()
            calls = (await conn.execute(select(tool_calls).where(tool_calls.c.conversation_id == conversation_id)
                                        .order_by(tool_calls.c.id))).mappings().all()
        by_turn: dict[int, list[str]] = {}
        for c in calls:
            by_turn.setdefault(c["turn"], []).append(c["name"])
        return {
            "conversation_id": conversation_id, "title": conv["title"], "channel": conv["channel"],
            "started_at": conv["started_at"].isoformat(), "turns": conv["turns"],
            "messages": [{"turn": m["turn"], "role": m["role"], "content": m["content"], "channel": m["channel"],
                          "interrupted": m["interrupted"], "created_at": m["created_at"].isoformat(),
                          "tools": by_turn.get(m["turn"], []) if m["role"] == "assistant" else []} for m in msgs],
        }

    # ---- login sessions (security-critical: written synchronously, not via the queue) --------------------

    async def create_auth_session(self, token_hash: str, customer_id: str, conversation_id: str, channel: str,
                                  user_agent: str | None, expires_at: datetime) -> None:
        ts = now()
        async with self.engine.begin() as conn:
            await conn.execute(insert(auth_sessions).values(
                token_hash=token_hash, customer_id=customer_id, conversation_id=conversation_id, channel=channel,
                user_agent=(user_agent or "")[:200], created_at=ts, last_seen_at=ts, expires_at=expires_at))

    async def get_auth_session(self, token_hash: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(auth_sessions).where(auth_sessions.c.token_hash == token_hash))).mappings().first()
        return dict(row) if row else None

    def touch_auth_session(self, token_hash: str, channel: str) -> None:
        self._put(("auth_touch", token_hash, {"last_seen_at": now(), "channel": channel}))

    async def revoke_auth_session(self, token_hash: str, reason: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(auth_sessions).where(auth_sessions.c.token_hash == token_hash,
                                                           auth_sessions.c.revoked_at.is_(None))
                               .values(revoked_at=now(), revoked_reason=reason))

    async def count(self, table: str, **where) -> int:
        await self.flush()
        t = TABLES[table]
        q = select(t)
        for k, v in where.items():
            q = q.where(t.c[k] == v)
        async with self.engine.connect() as conn:
            return len((await conn.execute(q)).all())


class NullStore:
    """Used where no storage is configured (e.g. ad-hoc scripts): accepts writes, stores nothing."""
    backend = "none"

    def record(self, table: str, **row) -> None:
        pass

    def touch(self, *a, **k) -> None:
        pass
