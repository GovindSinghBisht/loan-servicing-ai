"""HTTP API for the web app.

    uv run uvicorn engine.server:app --port 8000

Each customer message streams back as server-sent events, so the UI shows progress (which systems
are being checked) and the reply token by token instead of a spinner:

    tool         {name, label}                       a tool started
    tool_result  {name, ok, error, sources}          a tool finished
    action       {action_id, summary, details}       an action awaits the customer's confirmation
    text         {delta}                             reply text
    done         {text, metrics, sources, backend}   turn complete
    error        {message}

Sessions live in memory with an idle timeout; in production they would move to Redis so any API
instance can serve any conversation.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import Agent  # noqa: E402
from .clients import NotFound, Systems  # noqa: E402
from .core import Session, execute  # noqa: E402
from .llm import Provider  # noqa: E402
import engine.tools  # noqa: E402,F401

DEMO_OTP = "123456"  # mock OTP; a real deployment verifies via the lender's OTP service
IDLE_TIMEOUT_S = 30 * 60
DEMO_CUSTOMERS = [
    {"phone": "+919800000001", "name": "Priya Sharma", "story": "Home loan EMI went up; two failed debits"},
    {"phone": "+919800000002", "name": "Arjun Mehta", "story": "Personal loan overdue; card late fee"},
    {"phone": "+919800000003", "name": "Kavya Iyer", "story": "Duplicate EMI debit; card dispute"},
    {"phone": "+919800000004", "name": "Rohan Gupta", "story": "Two loans; exploring prepayment"},
]
TOOL_LABELS = {
    "get_customer_overview": "Loading your accounts",
    "get_loan_details": "Checking loan details",
    "explain_emi_change": "Comparing your EMIs and rate history",
    "get_payment_history": "Checking payment records",
    "prepayment_quote": "Calculating prepayment savings",
    "foreclosure_quote": "Calculating foreclosure amount",
    "get_loan_options": "Checking what you're eligible for",
    "check_charge_waiver": "Checking waiver eligibility",
    "request_charge_waiver": "Preparing waiver request",
    "request_prepayment": "Preparing prepayment request",
    "raise_payment_dispute": "Preparing payment dispute",
    "list_service_requests": "Checking your service requests",
    "escalate_service_request": "Preparing escalation",
    "confirm_action": "Submitting your request",
    "cancel_action": "Cancelling",
}

state: dict = {}
systems_factory = Systems  # tests swap in in-process systems


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["systems"] = systems_factory()
    state["sessions"] = {}
    yield
    await state["systems"].close()


app = FastAPI(title="Loan servicing assistant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_methods=["GET", "POST", "DELETE"], allow_headers=["content-type"],
)

# Hook for tests and offline UI work: when set, new sessions use this provider instead of a real LLM.
provider_factory = None


class Live:
    def __init__(self, agent: Agent):
        self.agent, self.lock, self.last_used = agent, asyncio.Lock(), time.monotonic()


def _get(sid: str) -> Live:
    sessions: dict[str, Live] = state["sessions"]
    now = time.monotonic()
    for k in [k for k, v in sessions.items() if now - v.last_used > IDLE_TIMEOUT_S]:
        del sessions[k]
    live = sessions.get(sid)
    if not live:
        raise HTTPException(404, "Session expired. Please sign in again.")
    live.last_used = now
    return live


@app.get("/api/health")
async def health():
    return {"status": "ok", "business_date": await state["systems"].business_date()}


@app.get("/api/demo-customers")
def demo_customers():
    return {"customers": DEMO_CUSTOMERS, "otp_hint": DEMO_OTP}


class Login(BaseModel):
    phone: str
    otp: str
    channel: Literal["text", "voice"] = "text"


@app.post("/api/login")
async def login(body: Login):
    if body.otp != DEMO_OTP:
        raise HTTPException(401, "Incorrect OTP.")
    systems: Systems = state["systems"]
    try:
        customer = await systems.crm.get("/customers/lookup", phone=body.phone)
    except NotFound:
        raise HTTPException(404, "No customer is registered with this mobile number.")
    session = Session(customer_id=customer["customer_id"], systems=systems, channel=body.channel)
    provider: Provider | None = provider_factory() if provider_factory else None
    agent = Agent(session, provider=provider)
    snapshot = await agent.start()
    sid = secrets.token_urlsafe(24)
    state["sessions"][sid] = Live(agent)
    session.log("login", channel=body.channel)
    return {"session_id": sid, "customer": snapshot.get("customer"), "account": snapshot}


@app.get("/api/sessions/{sid}/account")
async def account(sid: str):
    live = _get(sid)
    r = await execute(live.agent.session, "get_customer_overview", {})
    if not r["ok"]:
        raise HTTPException(503, r.get("message", "Account data unavailable"))
    return r["data"]


@app.delete("/api/sessions/{sid}")
def logout(sid: str):
    state["sessions"].pop(sid, None)
    return {"ok": True}


class Message(BaseModel):
    text: str


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/sessions/{sid}/messages")
async def message(sid: str, body: Message):
    live = _get(sid)
    text = body.text.strip()[:2000]
    if not text:
        raise HTTPException(422, "Empty message")
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    sources: list[str] = []

    def on_tool(name: str, _input: dict) -> None:
        queue.put_nowait(_sse("tool", {"name": name, "label": TOOL_LABELS.get(name, "Working on it")}))

    def on_tool_result(name: str, out: dict) -> None:
        sources.extend(s for s in out.get("sources", []) if s not in sources)
        queue.put_nowait(_sse("tool_result", {"name": name, "ok": out["ok"], "error": out.get("error"),
                                              "system": out.get("system"), "sources": out.get("sources", [])}))
        if out.get("status") == "CONFIRMATION_REQUIRED":
            queue.put_nowait(_sse("action", {k: out[k] for k in ("action_id", "summary", "details")}))

    async def run() -> None:
        try:
            async with live.lock:  # one turn at a time per conversation
                r = await live.agent.respond(text, on_text=lambda d: queue.put_nowait(_sse("text", {"delta": d})),
                                             on_tool=on_tool, on_tool_result=on_tool_result)
            backend = {}
            for s in r.spans:
                b = backend.setdefault(s.system, {"calls": 0, "ms": 0.0, "retries": 0, "failed": 0})
                b["calls"] += 1
                b["ms"] = max(b["ms"], s.ms)
                b["retries"] += max(s.attempts - 1, 0)
                b["failed"] += s.outcome in ("unavailable", "circuit_open")
            queue.put_nowait(_sse("done", {"text": r.text, "metrics": asdict(r.metrics), "sources": sources,
                                           "backend": backend, "refused": r.refused,
                                           "pending_actions": list(live.agent.session.pending)}))
        except Exception as e:  # surface failures to the UI instead of a dropped stream
            live.agent.session.log("turn_error", error=f"{type(e).__name__}: {e}")
            queue.put_nowait(_sse("error", {"message": "Sorry, something went wrong on our side. Please try again."}))
        finally:
            queue.put_nowait(None)

    async def stream():
        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
