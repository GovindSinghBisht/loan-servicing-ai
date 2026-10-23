"""Tool registry, session context and the confirmation gate for actions.

Security model:
- The authenticated customer_id lives on the Session and is injected into every tool. The LLM never
  supplies it, so it cannot ask for another customer's data; tools also verify ownership of any
  loan/card/ticket id the LLM passes and answer NOT_FOUND (not FORBIDDEN) to avoid leaking existence.
- Action tools never execute directly. They return a Proposal; the engine parks it and the LLM must
  call confirm_action in a *later* turn, after the customer has replied. The LLM cannot self-confirm.
- Before any action is proposed, governance policy is checked in code (kill switches, channel rules,
  per-session limits, amount caps); the model cannot talk its way past it.
- Every tool call, executed action and guardrail decision is recorded (database audit tables) and traced.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

from .clients import NotFound, Systems, SystemUnavailable
from .governance import GuardrailEvent, PolicyViolation, check_action
from .observability import tracer
from .storage import NullStore


class ToolError(Exception):
    """A business-level refusal the LLM should relay, e.g. NOT_ELIGIBLE or NOT_FOUND."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


@dataclass
class ToolResult:
    data: dict
    sources: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    """What an action tool returns instead of acting: a preview plus the deferred write."""
    summary: str
    details: dict
    run: Callable[[], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., Awaitable[ToolResult | Proposal]]
    kind: Literal["read", "action"]
    packs: frozenset[str]

    def schema(self) -> dict:
        s = self.input_model.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}


REGISTRY: dict[str, ToolSpec] = {}


class NoArgs(BaseModel):
    pass


def tool(description: str, input_model: type[BaseModel] = NoArgs, kind: Literal["read", "action"] = "read",
         packs: tuple[str, ...] = ("core",)):
    def register(fn):
        REGISTRY[fn.__name__] = ToolSpec(fn.__name__, description, input_model, fn, kind, frozenset(packs))
        return fn
    return register


@dataclass
class PendingAction:
    action_id: str
    tool: str
    proposal: Proposal
    proposed_in_turn: int


@dataclass
class Session:
    customer_id: str
    systems: Systems
    channel: Literal["text", "voice"] = "text"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turn: int = 0
    pending: dict[str, PendingAction] = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)
    store: Any = field(default_factory=NullStore)
    executed_actions: int = 0
    turn_guardrails: list[GuardrailEvent] = field(default_factory=list)

    def begin_turn(self) -> None:
        self.turn += 1
        self.turn_guardrails = []

    def log(self, event: str, **data) -> None:
        entry = {"ts": time.time(), "session_id": self.session_id, "customer_id": self.customer_id,
                 "turn": self.turn, "event": event, **data}
        self.audit.append(entry)
        self.store.record("audit_events", conversation_id=self.session_id, customer_id=self.customer_id,
                          turn=self.turn, event=event, data=jsonable(data))

    def guardrail(self, e: GuardrailEvent) -> None:
        """Record a guardrail decision for audit, the current turn's result and the trace."""
        self.turn_guardrails.append(e)
        self.store.record("guardrail_events", conversation_id=self.session_id, customer_id=self.customer_id,
                          turn=self.turn, stage=e.stage, rule=e.rule, severity=e.severity, detail=jsonable(e.detail))
        with tracer.observe(f"guardrail:{e.rule}", as_type="guardrail", input={"stage": e.stage},
                            output={"severity": e.severity, **e.detail},
                            level="WARNING" if e.severity != "info" else "DEFAULT"):
            pass


async def execute(session: Session, name: str, raw_args: dict) -> dict:
    """Run one tool call and return a JSON-serialisable result for the LLM. Never raises."""
    started = time.perf_counter()
    spec = REGISTRY.get(name)
    try:
        if spec is None:
            raise ToolError("UNKNOWN_TOOL", f"No tool named {name}")
        args = spec.input_model.model_validate(raw_args or {})
        if spec.kind == "action":
            check_action(name, args.model_dump(), session.channel, session.executed_actions)
        with tracer.observe(name, as_type="tool", input=raw_args) as span:
            out = await spec.handler(session, args)
            span.update(output=out.summary if isinstance(out, Proposal) else out.data)
        if isinstance(out, Proposal):
            action_id = f"ACT-{uuid.uuid4().hex[:8]}"
            session.pending[action_id] = PendingAction(action_id, name, out, session.turn)
            result = {"ok": True, "status": "CONFIRMATION_REQUIRED", "action_id": action_id,
                      "summary": out.summary, "details": out.details,
                      "instruction": "Read the summary to the customer and ask them to confirm. Call confirm_action "
                                     "only after they explicitly agree in their next message."}
        else:
            result = {"ok": True, "data": out.data, "sources": out.sources}
    except ValidationError as e:
        result = {"ok": False, "error": "INVALID_ARGUMENTS", "message": str(e.errors()[:3])}
    except PolicyViolation as e:
        session.guardrail(e.event)
        result = {"ok": False, "error": "POLICY_BLOCKED", "rule": e.event.rule, "message": e.message}
    except ToolError as e:
        result = {"ok": False, "error": e.code, "message": e.message, **e.details}
    except NotFound as e:
        result = {"ok": False, "error": "NOT_FOUND", "message": f"No matching record in {e.system}."}
    except SystemUnavailable as e:
        result = {"ok": False, "error": "SYSTEM_UNAVAILABLE", "system": e.system,
                  "message": f"The {e.system} system is not responding right now. Share what you can from other "
                             f"systems and offer to follow up; do not guess the missing data."}
    session.log("tool_call", tool=name, args=raw_args, ok=result["ok"], error=result.get("error"),
                ms=round((time.perf_counter() - started) * 1000, 1))
    return result


def tools_for(packs: set[str]) -> list[ToolSpec]:
    return [t for t in REGISTRY.values() if t.packs & packs]


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))
