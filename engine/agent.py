"""The conversation loop: the LLM plans and explains, tools fetch and compute.

A hand-written loop because the engine owns things the loop must see: the turn counter that gates
confirmations, parallel execution of independent tool calls, per-step latency, and token streaming
that the voice channel reuses. The LLM vendor sits behind engine.llm.Provider.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .clients import Span, start_trace
from .core import Session, execute, tools_for
from .governance import Grounding, check_input, check_output
from .llm import Prompt, Provider, ToolCall, make_provider
from .observability import tracer
from .packs import CHANNEL_PROMPTS, PACKS, PLATFORM_PROMPT, Pack, compose

MAX_STEPS = 8
WRITE_TOOLS = {"confirm_action"}  # once started, these finish even if the customer interrupts
INTERRUPTED = {"ok": False, "error": "INTERRUPTED", "message": "The customer interrupted before this finished."}


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
    guardrails: list[dict] = field(default_factory=list)


class Agent:
    def __init__(self, session: Session, pack: Pack | str | list[str] = "loans", provider: Provider | None = None):
        self.session = session
        self.pack = compose(pack) if isinstance(pack, list) else PACKS[pack] if isinstance(pack, str) else pack
        self.provider = provider or make_provider(tools_for(set(self.pack.tool_groups)))
        self.history: list = []
        self.snapshot: dict | None = None
        self._interrupted = False
        self.grounding = Grounding()  # evidence a reply may cite: snapshot + every tool result this conversation

    async def start(self, overview: dict | None = None) -> dict:
        """Load the customer snapshot once per session so most turns start with context already in hand.
        Pass an overview result already fetched (e.g. to choose packs) to avoid loading it twice."""
        if overview is None:
            start_trace()
            overview = await execute(self.session, "get_customer_overview", {})
        self.snapshot = overview.get("data") or {"unavailable": overview.get("message")}
        self.grounding.add(self.snapshot)
        return self.snapshot

    def _prompt(self) -> Prompt:
        return Prompt(
            stable=f"{PLATFORM_PROMPT}\n\n{self.pack.instructions}\n\n{CHANNEL_PROMPTS[self.session.channel]}",
            dynamic="Customer snapshot at the start of this conversation (refresh with tools if you need detail "
                    "or the customer has just made a change):\n" + json.dumps(self.snapshot, default=str),
        )

    async def respond(self, user_text: str, on_text: Callable[[str], None] | None = None,
                      on_tool: Callable[[str, dict], None] | None = None,
                      on_tool_result: Callable[[str, dict], None] | None = None) -> TurnResult:
        if self.snapshot is None:
            await self.start()
        s = self.session
        s.begin_turn()
        spans = start_trace()
        started = time.perf_counter()
        m = TurnMetrics()
        result = TurnResult(text="", spans=spans, metrics=m)
        texts: list[str] = []

        def stream_text(t: str) -> None:
            if m.first_text_ms is None:
                m.first_text_ms = round((time.perf_counter() - started) * 1000, 1)
            texts.append(t)
            if on_text:
                on_text(t)

        # Input guardrails run before the model sees anything; redacted text replaces the original everywhere.
        decision = check_input(user_text)
        for e in decision.events:
            s.guardrail(e)
        packs = sorted(self.pack.tool_groups - {"core"})
        with tracer.turn(session_id=s.session_id, customer_id=s.customer_id, channel=s.channel, packs=packs,
                         user_text=decision.text) as root:
            s.store.record("messages", conversation_id=s.session_id, turn=s.turn, role="user", content=decision.text,
                           channel=s.channel, guardrails=[e.rule for e in decision.events] or None)
            if decision.blocked_reply:
                stream_text(decision.blocked_reply)  # the model never sees a message carrying a secret
            else:
                note = ""
                if self._interrupted:
                    note = "[The customer interrupted your previous reply, so they may not have heard all of it.] "
                    self._interrupted = False
                self.history += self.provider.user_message(note + decision.text)
                in_flight: list[tuple[ToolCall, asyncio.Future]] = []
                try:
                    await self._loop(m, result, stream_text, on_tool, on_tool_result, in_flight)
                except asyncio.CancelledError:
                    said = "".join(texts)
                    await self._repair_after_interrupt(in_flight, said)
                    self._save_turn(result, said.strip(), interrupted=True, title=decision.text)
                    s.log("turn_interrupted", tools=[c["name"] for c in result.tool_calls])
                    raise
                for e in check_output("".join(texts), self.grounding):
                    s.guardrail(e)

            m.total_ms = round((time.perf_counter() - started) * 1000, 1)
            m.llm_ms, m.tools_ms = round(m.llm_ms, 1), round(m.tools_ms, 1)
            result.text = "".join(texts).strip()
            result.guardrails = [asdict(e) for e in s.turn_guardrails]
            self._save_turn(result, result.text, interrupted=False, title=decision.text)
            root.update(output=result.text, metadata={"metrics": asdict(m), "guardrails": [e.rule for e in s.turn_guardrails]})
        # Audit keeps metadata only; message content lives in the retention-managed conversation tables.
        s.log("turn", tools=[c["name"] for c in result.tool_calls], provider=self.provider.name,
              model=self.provider.model, metrics=asdict(m), guardrails=[e.rule for e in s.turn_guardrails])
        return result

    def _save_turn(self, result: TurnResult, reply: str, interrupted: bool, title: str) -> None:
        s, m = self.session, result.metrics
        s.store.record("messages", conversation_id=s.session_id, turn=s.turn, role="assistant", content=reply,
                       channel=s.channel, interrupted=interrupted,
                       guardrails=[e.rule for e in s.turn_guardrails if e.stage == "output"] or None)
        for c in result.tool_calls:
            s.store.record("tool_calls", conversation_id=s.session_id, turn=s.turn, name=c["name"], args=c["input"],
                           ok=c["ok"], error=c["error"], status=c["status"])
        s.store.record("turn_metrics", conversation_id=s.session_id, turn=s.turn, first_text_ms=m.first_text_ms,
                       total_ms=m.total_ms, llm_ms=m.llm_ms, tools_ms=m.tools_ms, steps=m.steps,
                       input_tokens=m.input_tokens, output_tokens=m.output_tokens, cached_tokens=m.cached_tokens)
        s.store.touch(s.session_id, s.turn, title=title if s.turn == 1 else None)

    async def _loop(self, m: TurnMetrics, result: TurnResult, stream_text, on_tool, on_tool_result,
                    in_flight: list) -> None:
        for _ in range(MAX_STEPS):
            m.steps += 1
            llm_started = time.perf_counter()
            with tracer.observe("llm", as_type="generation", model=self.provider.model,
                                input={"step": m.steps, "messages": len(self.history)}) as gen:
                step = await self.provider.step(self._prompt(), self.history, stream_text)
                gen.update(output=step.text or [c.name for c in step.tool_calls],
                           usage_details={"input": step.input_tokens, "output": step.output_tokens})
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
            in_flight[:] = [(c, asyncio.ensure_future(self._run_tool(c))) for c in step.tool_calls]
            outputs = await asyncio.gather(*(asyncio.shield(t) if c.name in WRITE_TOOLS else t for c, t in in_flight))
            in_flight.clear()
            m.tools_ms += (time.perf_counter() - tools_started) * 1000
            for o in outputs:
                self.grounding.add(o)
            if on_tool_result:
                for c, o in zip(step.tool_calls, outputs):
                    on_tool_result(c.name, o)
            self.history += self.provider.tool_results(step.tool_calls, outputs)
            result.tool_calls += [{"name": c.name, "input": c.input, "ok": o["ok"], "error": o.get("error"),
                                   "status": o.get("status")} for c, o in zip(step.tool_calls, outputs)]

    async def _repair_after_interrupt(self, in_flight: list[tuple[ToolCall, asyncio.Future]], said: str) -> None:
        """Keep the history valid after a barge-in: every tool call gets a result, writes report what really happened."""
        self._interrupted = True
        if in_flight:
            outputs = []
            for call, task in in_flight:
                if call.name in WRITE_TOOLS and not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=10)
                    except Exception:
                        pass
                if task.done() and not task.cancelled() and task.exception() is None:
                    outputs.append(task.result())
                else:
                    task.cancel()
                    outputs.append(INTERRUPTED)
            self.history += self.provider.tool_results([c for c, _ in in_flight], outputs)
            in_flight.clear()
        elif said.strip():
            # The reply was cut off mid-sentence; keep what the customer actually heard.
            self.history.append({"role": "assistant", "content": said.strip() + " …"})

    async def _run_tool(self, call: ToolCall) -> dict:
        if call.input is None:
            return {"ok": False, "error": "INVALID_ARGUMENTS", "message": "Tool arguments were not a JSON object."}
        return await execute(self.session, call.name, call.input)
