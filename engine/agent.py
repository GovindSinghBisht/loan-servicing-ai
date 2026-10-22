"""The conversation loop: the LLM plans and explains, tools fetch and compute.

A hand-written loop because the engine owns things the loop must see: the turn counter that gates
confirmations, parallel execution of independent tool calls, per-step latency, and token streaming
that the voice channel reuses. The LLM vendor sits behind engine.llm.Provider.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from .clients import Span, start_trace
from .core import Session, execute, tools_for
from .llm import Prompt, Provider, ToolCall, make_provider
from .packs import CHANNEL_PROMPTS, PACKS, PLATFORM_PROMPT, Pack

MAX_STEPS = 8


@dataclass
class TurnMetrics:
    first_text_ms: float | None = None   # user message -> first streamed token of the reply
    total_ms: float = 0.0
    llm_ms: float = 0.0
    tools_ms: float = 0.0
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class TurnResult:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    metrics: TurnMetrics = field(default_factory=TurnMetrics)
    refused: bool = False


class Agent:
    def __init__(self, session: Session, pack: Pack | str = "loans", provider: Provider | None = None):
        self.session = session
        self.pack = PACKS[pack] if isinstance(pack, str) else pack
        self.provider = provider or make_provider(tools_for(set(self.pack.tool_groups)))
        self.history: list = []
        self.snapshot: dict | None = None

    async def start(self) -> dict:
        """Load the customer snapshot once per session so most turns start with context already in hand."""
        start_trace()
        r = await execute(self.session, "get_customer_overview", {})
        self.snapshot = r.get("data") or {"unavailable": r.get("message")}
        return self.snapshot

    def _prompt(self) -> Prompt:
        return Prompt(
            stable=f"{PLATFORM_PROMPT}\n\n{self.pack.instructions}\n\n{CHANNEL_PROMPTS[self.session.channel]}",
            dynamic="Customer snapshot at the start of this conversation (refresh with tools if you need detail "
                    "or the customer has just made a change):\n" + json.dumps(self.snapshot, default=str),
        )

    async def respond(self, user_text: str, on_text: Callable[[str], None] | None = None,
                      on_tool: Callable[[str, dict], None] | None = None) -> TurnResult:
        if self.snapshot is None:
            await self.start()
        self.session.begin_turn()
        spans = start_trace()
        started = time.perf_counter()
        m = TurnMetrics()
        result = TurnResult(text="", spans=spans, metrics=m)
        self.history += self.provider.user_message(user_text)
        texts: list[str] = []

        def stream_text(t: str) -> None:
            if m.first_text_ms is None:
                m.first_text_ms = round((time.perf_counter() - started) * 1000, 1)
            texts.append(t)
            if on_text:
                on_text(t)

        for _ in range(MAX_STEPS):
            m.steps += 1
            llm_started = time.perf_counter()
            step = await self.provider.step(self._prompt(), self.history, stream_text)
            m.llm_ms += (time.perf_counter() - llm_started) * 1000
            m.input_tokens += step.input_tokens
            m.output_tokens += step.output_tokens
            m.cached_tokens += step.cached_tokens
            self.history += step.assistant_message  # append-only history

            if step.stop == "refusal":
                result.refused = True
                stream_text("I'm not able to help with that here. I can connect you to a member of our team.")
                break
            if not step.tool_calls:
                break
            if step.stop == "max_tokens":
                raise RuntimeError("Tool input was truncated by the output token limit")

            for c in step.tool_calls:
                if on_tool:
                    on_tool(c.name, c.input or {})
            tools_started = time.perf_counter()
            outputs = await asyncio.gather(*(self._run_tool(c) for c in step.tool_calls))
            m.tools_ms += (time.perf_counter() - tools_started) * 1000
            self.history += self.provider.tool_results(step.tool_calls, outputs)
            result.tool_calls += [{"name": c.name, "input": c.input, "ok": o["ok"], "error": o.get("error"),
                                   "status": o.get("status")} for c, o in zip(step.tool_calls, outputs)]

        m.total_ms = round((time.perf_counter() - started) * 1000, 1)
        m.llm_ms, m.tools_ms = round(m.llm_ms, 1), round(m.tools_ms, 1)
        result.text = "".join(texts).strip()
        self.session.log("turn", user=user_text, reply=result.text, tools=[c["name"] for c in result.tool_calls],
                         provider=self.provider.name, model=self.provider.model, metrics=m.__dict__)
        return result

    async def _run_tool(self, call: ToolCall) -> dict:
        if call.input is None:
            return {"ok": False, "error": "INVALID_ARGUMENTS", "message": "Tool arguments were not a JSON object."}
        return await execute(self.session, call.name, call.input)
