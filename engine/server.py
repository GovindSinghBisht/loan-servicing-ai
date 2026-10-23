"""HTTP API for the web app.

    uv run uvicorn engine.server:app --port 8000

Each customer message streams back as server-sent events, so the UI shows progress (which systems
are being checked) and the reply token by token instead of a spinner:

    tool         {name, label}                       a tool started
    filler       {text}                              voice only: something to say while lookups run
    tool_result  {name, ok, error, sources}          a tool finished
    action       {action_id, summary, details}       an action awaits the customer's confirmation
    text         {delta}                             reply text
    done         {text, metrics, sources, backend}   turn complete
    error        {message}

Sign-in creates a login session: a random bearer token returned once to the browser and stored server
side only as a SHA-256 hash, with idle and absolute expiry from the governance policy. Requests send it
in the Authorization header (never in URLs, so it cannot leak into access logs). Active sessions are
checked in memory; after a restart, or on another instance, a valid session is restored from the
database together with its conversation so far. Sign-out revokes the session (kept for audit).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import Agent  # noqa: E402
from .clients import NotFound, Systems  # noqa: E402
from .core import Session, execute  # noqa: E402
from .governance import GuardrailEvent, check_model, policy  # noqa: E402
from .observability import tracer  # noqa: E402
from .packs import packs_for  # noqa: E402
from .storage import Store  # noqa: E402
from .llm import Provider  # noqa: E402
import engine.tools  # noqa: E402,F401

DEMO_OTP = "123456"  # mock OTP; a real deployment verifies via the lender's OTP service
TOUCH_EVERY_S = 60   # how often a session's last-seen time is persisted
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
    "get_card_details": "Checking your card",
    "explain_card_charges": "Checking your card statements and fee rules",
    "get_card_transactions": "Checking card transactions",
    "check_late_fee_reversal": "Checking late fee reversal eligibility",
    "request_late_fee_reversal": "Preparing late fee reversal",
    "raise_card_dispute": "Preparing transaction dispute",
    "block_card": "Preparing card block",
    "confirm_action": "Submitting your request",
    "cancel_action": "Cancelling",
}

# Voice: said while tools run, so slow backends don't leave silence. Only before the reply starts.
VOICE_FILLERS = {
    "explain_emi_change": "Let me compare your last two EMIs.",
    "get_payment_history": "Let me check your payment records.",
    "prepayment_quote": "Let me work out the numbers for that.",
    "foreclosure_quote": "Let me calculate the closure amount.",
    "get_loan_options": "Let me see what options you have.",
    "list_service_requests": "Let me pull up your requests.",
    "explain_card_charges": "Let me look at your card statements.",
    "get_card_transactions": "Let me check your card transactions.",
    "confirm_action": "Submitting that now.",
}
DEFAULT_FILLER = "One moment, let me check."
SLOW_FILLER = "Still checking, thanks for waiting."
FILLER_AFTER_S = 0.4       # quick lookups need no filler; it would only delay the answer
SLOW_FILLER_AFTER_S = 2.5
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "coral")
TTS_STYLE = ("Speak warmly and clearly, like a helpful customer service agent at an Indian bank. "
             "Natural, moderate pace. Read amounts in rupees naturally.")
state: dict = {}
systems_factory = Systems  # tests swap in in-process systems
store_factory = Store      # tests swap in a temporary database


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["systems"] = systems_factory()
    state["store"] = store_factory()
    await state["store"].init()
    state["sessions"] = {}
    state["background"] = set()
    yield
    await asyncio.gather(*state["background"], return_exceptions=True)
    await state["store"].close()
    await state["systems"].close()
    tracer.flush()


app = FastAPI(title="Loan servicing assistant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(","),
    allow_methods=["GET", "POST"], allow_headers=["content-type", "authorization"],
)

# Hook for tests and offline UI work: when set, new sessions use this provider instead of a real LLM.
provider_factory = None


class Live:
    """An active login session: the conversation agent plus its expiry bookkeeping."""

    def __init__(self, agent: Agent, token_hash: str, expires_at: datetime):
        self.agent, self.token_hash, self.expires_at = agent, token_hash, expires_at
        self.lock, self.last_used, self.last_persisted = asyncio.Lock(), time.monotonic(), time.monotonic()
        self.past: list[dict] | None = None  # earlier conversations, fetched once in the background
        self.prefetch = asyncio.create_task(self._load_past())
        state["background"].add(self.prefetch)
        self.prefetch.add_done_callback(state["background"].discard)

    async def _load_past(self) -> None:
        try:
            rows = await state["store"].recent_conversations(self.agent.session.customer_id)
            self.past = [r for r in rows if r["conversation_id"] != self.agent.session.session_id]
        except Exception:
            self.past = None  # fall back to reading on demand


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _idle_s() -> float:
    return policy()["sessions"]["idle_minutes"] * 60


async def _new_agent(customer_id: str, channel: str, conversation_id: str | None = None) -> tuple[Agent, dict, list[str]]:
    systems: Systems = state["systems"]
    kwargs = {"session_id": conversation_id} if conversation_id else {}
    session = Session(customer_id=customer_id, systems=systems, channel=channel, store=state["store"], **kwargs)
    overview = await execute(session, "get_customer_overview", {})
    packs = packs_for(overview.get("data") or {})
    provider: Provider | None = provider_factory() if provider_factory else None
    agent = Agent(session, pack=packs, provider=provider)
    check_model(agent.provider.model)  # governance: only approved models serve customers
    return agent, await agent.start(overview), packs


async def _restore(token_hash: str) -> Live | None:
    """Rebuild a still-valid session (after a restart or on another instance), conversation included."""
    row = await state["store"].get_auth_session(token_hash)
    if not row or row["revoked_at"]:
        return None
    now = datetime.now(timezone.utc)
    expires_at, last_seen = row["expires_at"], row["last_seen_at"]
    if expires_at.tzinfo is None:  # SQLite returns naive timestamps
        expires_at, last_seen = expires_at.replace(tzinfo=timezone.utc), last_seen.replace(tzinfo=timezone.utc)
    if now >= expires_at or (now - last_seen).total_seconds() > _idle_s():
        await state["store"].revoke_auth_session(token_hash, "expired")
        return None
    agent, _, _ = await _new_agent(row["customer_id"], row["channel"] or "text", row["conversation_id"])
    transcript = await state["store"].transcript(row["conversation_id"], row["customer_id"])
    if transcript:
        agent.restore(transcript["messages"])
    agent.session.log("session_restored", turns=agent.session.turn)
    return Live(agent, token_hash, expires_at)


async def current(authorization: str | None = Header(None)) -> Live:
    """Resolve the bearer token to an active session, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in.")
    token_hash = _hash(authorization[7:].strip())
    sessions: dict[str, Live] = state["sessions"]
    live = sessions.get(token_hash)
    if live:
        idle = time.monotonic() - live.last_used > _idle_s()
        if idle or datetime.now(timezone.utc) >= live.expires_at:
            del sessions[token_hash]
            await state["store"].revoke_auth_session(token_hash, "idle_timeout" if idle else "expired")
            live = None
    else:
        live = await _restore(token_hash)
        if live:
            sessions[token_hash] = live
    if not live:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    live.last_used = time.monotonic()
    if live.last_used - live.last_persisted > TOUCH_EVERY_S:  # idle expiry is enforced in memory; the DB copy
        live.last_persisted = live.last_used                     # only matters for restores, so write it sparingly
        state["store"].touch_auth_session(token_hash, live.agent.session.channel)
    return live


def tts_backend() -> str:
    if os.environ.get("VOICE_TTS", "openai") == "openai" and os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "browser"


@app.get("/api/health")
async def health():
    return {"status": "ok", "business_date": await state["systems"].business_date(), "tts": tts_backend(),
            "storage": state["store"].backend, "tracing": "langfuse" if tracer.enabled else "off"}


@app.get("/api/demo-customers")
def demo_customers():
    return {"customers": DEMO_CUSTOMERS, "otp_hint": DEMO_OTP}


class Login(BaseModel):
    phone: str
    otp: str
    channel: Literal["text", "voice"] = "text"


@app.post("/api/login")
async def login(body: Login, request: Request):
    if body.otp != DEMO_OTP:
        raise HTTPException(401, "Incorrect OTP.")
    try:
        customer = await state["systems"].crm.get("/customers/lookup", phone=body.phone)
    except NotFound:
        raise HTTPException(404, "No customer is registered with this mobile number.")
    agent, snapshot, packs = await _new_agent(customer["customer_id"], body.channel)
    s = agent.session
    await state["store"].start_conversation(s.session_id, s.customer_id, body.channel, packs,
                                            agent.provider.name, agent.provider.model)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=policy()["sessions"]["absolute_hours"])
    await state["store"].create_auth_session(_hash(token), s.customer_id, s.session_id, body.channel,
                                             request.headers.get("user-agent"), expires_at)
    state["sessions"][_hash(token)] = Live(agent, _hash(token), expires_at)
    s.log("login", channel=body.channel, packs=packs)
    return {"token": token, "expires_at": expires_at.isoformat(), "idle_minutes": policy()["sessions"]["idle_minutes"],
            "customer": snapshot.get("customer"), "account": snapshot, "packs": packs}


@app.get("/api/me")
async def me(live: Live = Depends(current)):
    """Who is signed in, for restoring the app after a page refresh."""
    r = await execute(live.agent.session, "get_customer_overview", {})
    account = r.get("data") or live.agent.snapshot
    return {"customer": account.get("customer"), "account": account, "packs": sorted(live.agent.pack.tool_groups - {"core"}),
            "expires_at": live.expires_at.isoformat(), "turns": live.agent.session.turn}


@app.post("/api/logout")
async def logout(live: Live = Depends(current)):
    state["sessions"].pop(live.token_hash, None)
    await state["store"].revoke_auth_session(live.token_hash, "logout")
    live.agent.session.log("logout")
    return {"ok": True}


@app.get("/api/conversations")
async def conversations(live: Live = Depends(current)):
    """The signed-in customer's recent conversations (retention keeps the latest few). The current one is
    built from memory and earlier ones were prefetched at sign-in, so this rarely touches the database."""
    await live.prefetch
    if live.past is None:
        await live._load_past()
    keep = policy()["retention"]["conversations_per_customer"]
    rows = [live.agent.summary()] + [{**r, "current": False} for r in (live.past or [])]
    return {"conversations": rows[:keep]}


@app.get("/api/conversations/{conversation_id}")
async def conversation(conversation_id: str, live: Live = Depends(current)):
    agent = live.agent
    if conversation_id == agent.session.session_id:  # current conversation: served from memory
        return {"conversation_id": conversation_id, "title": agent.summary()["title"], "channel": agent.session.channel,
                "started_at": agent.started_at.isoformat(), "turns": agent.session.turn, "messages": agent.transcript}
    t = await state["store"].transcript(conversation_id, live.agent.session.customer_id)
    if not t:
        raise HTTPException(404, "Conversation not found.")
    return t


@app.get("/api/account")
async def account(live: Live = Depends(current)):
    r = await execute(live.agent.session, "get_customer_overview", {})
    if not r["ok"]:
        raise HTTPException(503, r.get("message", "Account data unavailable"))
    return r["data"]


class Channel(BaseModel):
    channel: Literal["text", "voice"]


@app.post("/api/channel")
async def set_channel(body: Channel, live: Live = Depends(current)):
    """Same conversation, different medium: only the reply style changes, tools and history carry over."""
    consent = policy()["channels"].get("voice_requires_consent")
    if body.channel == "voice" and consent:
        profile = await state["systems"].crm.get(f"/customers/{live.agent.session.customer_id}")
        if not profile.get("consents", {}).get(consent):
            live.agent.session.guardrail(GuardrailEvent("channel", "voice_consent_missing", "block", {"consent": consent}))
            raise HTTPException(403, "Voice isn't available: call recording consent hasn't been given.")
    live.agent.session.channel = body.channel
    live.agent.session.log("channel", channel=body.channel)
    return {"channel": body.channel}


class Speech(BaseModel):
    text: str


@app.post("/api/tts")
async def tts(body: Speech, _: Live = Depends(current)):  # signed-in only, so this is not an open TTS proxy
    if tts_backend() != "openai":
        raise HTTPException(503, "Server speech is not configured; use the browser voice.")
    text = body.text.strip()[:600]
    if not text:
        raise HTTPException(422, "Nothing to say")
    if "openai" not in state:
        from openai import AsyncOpenAI
        state["openai"] = AsyncOpenAI()
    try:
        audio = await state["openai"].audio.speech.create(
            model=TTS_MODEL, voice=TTS_VOICE, input=text, instructions=TTS_STYLE, response_format="mp3")
    except Exception as e:
        raise HTTPException(502, f"Speech service error: {type(e).__name__}")
    return Response(content=audio.content, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


class VoiceMetrics(BaseModel):
    """Client-side timings for one voice turn, in ms, measured from the end of the customer's speech."""
    transcript_ms: float | None = None       # speech end -> final transcript
    first_audio_ms: float | None = None      # speech end -> first sound (filler or answer)
    first_answer_audio_ms: float | None = None
    first_token_ms: float | None = None      # request sent -> first reply token (server)
    turn_total_ms: float | None = None
    interrupted: bool = False
    tts: str | None = None


@app.post("/api/voice-metrics")
async def voice_metrics(body: VoiceMetrics, live: Live = Depends(current)):
    s = live.agent.session
    s.store.record("voice_metrics", conversation_id=s.session_id, customer_id=s.customer_id, data=body.model_dump())
    return {"ok": True}


class Message(BaseModel):
    text: str


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/messages")
async def message(body: Message, live: Live = Depends(current)):
    text = body.text.strip()[:2000]
    if not text:
        raise HTTPException(422, "Empty message")
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    sources: list[str] = []
    voice = live.agent.session.channel == "voice"
    turn = {"spoke": False, "filler": False, "done": False}

    def say_filler(text: str) -> None:
        if not (turn["spoke"] or turn["done"]):
            queue.put_nowait(_sse("filler", {"text": text}))

    def on_tool(name: str, _input: dict) -> None:
        queue.put_nowait(_sse("tool", {"name": name, "label": TOOL_LABELS.get(name, "Working on it")}))
        if voice and not turn["filler"]:
            turn["filler"] = True
            loop = asyncio.get_running_loop()
            loop.call_later(FILLER_AFTER_S, say_filler, VOICE_FILLERS.get(name, DEFAULT_FILLER))
            loop.call_later(SLOW_FILLER_AFTER_S, say_filler, SLOW_FILLER)

    def on_text(delta: str) -> None:
        turn["spoke"] = True
        queue.put_nowait(_sse("text", {"delta": delta}))

    def on_tool_result(name: str, out: dict) -> None:
        sources.extend(s for s in out.get("sources", []) if s not in sources)
        queue.put_nowait(_sse("tool_result", {"name": name, "ok": out["ok"], "error": out.get("error"),
                                              "system": out.get("system"), "sources": out.get("sources", [])}))
        if out.get("status") == "CONFIRMATION_REQUIRED":
            queue.put_nowait(_sse("action", {k: out[k] for k in ("action_id", "summary", "details")}))

    async def run() -> None:
        try:
            async with live.lock:  # one turn at a time per conversation
                r = await live.agent.respond(text, on_text=on_text, on_tool=on_tool, on_tool_result=on_tool_result)
            backend = {}
            for s in r.spans:
                b = backend.setdefault(s.system, {"calls": 0, "ms": 0.0, "retries": 0, "failed": 0})
                b["calls"] += 1
                b["ms"] = max(b["ms"], s.ms)
                b["retries"] += max(s.attempts - 1, 0)
                b["failed"] += s.outcome in ("unavailable", "circuit_open")
            queue.put_nowait(_sse("done", {"text": r.text, "metrics": asdict(r.metrics), "sources": sources,
                                           "backend": backend, "refused": r.refused,
                                           "pending_actions": list(live.agent.session.pending),
                                           "guardrails": r.guardrails}))
        except Exception as e:  # surface failures to the UI instead of a dropped stream
            live.agent.session.log("turn_error", error=f"{type(e).__name__}: {e}")
            queue.put_nowait(_sse("error", {"message": "Sorry, something went wrong on our side. Please try again."}))
        finally:
            turn["done"] = True
            queue.put_nowait(None)

    async def stream():
        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            # The client went away mid-turn (e.g. the customer talked over the assistant): stop working on it.
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
