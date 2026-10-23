"""Tracing with Langfuse: one trace per conversation turn, grouped by session and customer.

Each turn records the model calls (model, token usage, latency), every tool call (arguments, outcome),
and guardrail decisions. Data is PII-masked before it leaves the process. Enabled only when
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (LANGFUSE_BASE_URL selects the region); otherwise
every call is a cheap no-op, so tests and offline runs need nothing.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from .governance import mask_for_traces, policy

log = logging.getLogger("observability")


class _Noop:
    def update(self, **_: Any) -> "_Noop":
        return self


class Tracer:
    def __init__(self):
        self.client = None
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
            try:
                from langfuse import Langfuse
                mask = (lambda *, data, **_: mask_for_traces(data)) if policy()["observability"]["mask_pii_in_traces"] else None
                self.client = Langfuse(mask=mask, environment=os.environ.get("APP_ENV", "development"))
            except Exception as e:  # observability must never stop the assistant
                log.warning("Langfuse disabled: %s", e)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @contextmanager
    def turn(self, *, session_id: str, customer_id: str, channel: str, packs: list[str], user_text: str) -> Iterator[Any]:
        """Root span for one turn; everything traced inside joins this trace and session."""
        if not self.client:
            yield _Noop()
            return
        from langfuse import propagate_attributes
        with self.client.start_as_current_observation(name="conversation-turn", as_type="agent", input=user_text) as root, \
                propagate_attributes(user_id=customer_id, session_id=session_id, trace_name="conversation-turn",
                                     tags=[channel, *packs]):
            yield root

    @contextmanager
    def observe(self, name: str, as_type: str = "span", **kwargs: Any) -> Iterator[Any]:
        if not self.client:
            yield _Noop()
            return
        with self.client.start_as_current_observation(name=name, as_type=as_type, **kwargs) as obs:
            yield obs

    def flush(self) -> None:
        if self.client:
            self.client.flush()


tracer = Tracer()
