"""Voice behaviour: barge-in cancels a turn without corrupting history, writes finish, fillers cover slow lookups."""
import asyncio
from contextlib import contextmanager

import mock_systems.payments
import mock_systems.service_ops
import mock_systems.loans
from engine import server
from engine.agent import Agent
from engine.llm import Step, ToolCall
from tests.test_server import H, api, events, login  # noqa: F401  (fixture + helpers)


@contextmanager
def slow(app, ms: float):
    """Make an in-process mock system slow for the duration of the block."""
    chaos = app.state.chaos
    saved = (chaos.enabled, chaos.p50_ms, chaos.p95_ms, chaos.error_rate, chaos.hang_rate)
    chaos.enabled, chaos.p50_ms, chaos.p95_ms, chaos.error_rate, chaos.hang_rate = True, ms, ms * 1.01, 0, 0
    try:
        yield
    finally:
        chaos.enabled, chaos.p50_ms, chaos.p95_ms, chaos.error_rate, chaos.hang_rate = saved


class Scripted:
    """Provider that replays tool calls, then answers; history in the offline provider's format."""
    name, model = "scripted", "test"

    def __init__(self, *steps):
        self.steps = list(steps)

    def user_message(self, text):
        return [{"role": "user", "content": text}]

    def tool_results(self, calls, outputs):
        return [{"role": "tool", "id": c.id, "name": c.name, "content": o} for c, o in zip(calls, outputs)]

    async def step(self, prompt, history, on_text=None):
        nxt = self.steps.pop(0)
        if callable(nxt):
            nxt = nxt()
        if isinstance(nxt, str):
            if on_text:
                on_text(nxt)
            return Step(nxt, [], "end", assistant_message=[{"role": "assistant", "content": nxt}])
        return Step("", nxt, "tool_use", assistant_message=[{"role": "assistant", "calls": [c.id for c in nxt]}])


def answered(history) -> bool:
    """Every tool call in history has a result: the invariant the LLM APIs enforce."""
    asked = [i for m in history if m["role"] == "assistant" for i in m.get("calls", [])]
    got = [m["id"] for m in history if m["role"] == "tool"]
    return asked == got


async def test_barge_in_during_slow_lookup_keeps_history_valid(session):
    provider = Scripted([ToolCall("t1", "get_payment_history", {"loan_id": "HL1000001"})], "Answer after interrupt.")
    agent = Agent(session("CUST100001"), provider=provider)
    with slow(mock_systems.payments.app, 2000):
        turn = asyncio.create_task(agent.respond("Did any payment fail?"))
        await asyncio.sleep(0.2)
        turn.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            pass
    assert answered(agent.history)
    assert agent.history[-1]["content"]["error"] == "INTERRUPTED"

    r = await agent.respond("Actually, what's my next EMI?")
    assert r.text == "Answer after interrupt."
    assert agent.history[-2]["content"].startswith("[The customer interrupted your previous reply")


async def test_barge_in_during_confirmation_still_completes_the_write(session):
    s = session("CUST100003")
    agent = Agent(s, provider=Scripted(
        [ToolCall("t1", "escalate_service_request", {"ticket_id": "SR0000001", "reason": "refund late"})],
        "Shall I escalate?",
        lambda: [ToolCall("t2", "confirm_action", {"action_id": next(iter(s.pending))})],
        "Escalated.",
    ))
    await agent.respond("Please escalate my dispute")
    with slow(mock_systems.service_ops.app, 600):
        turn = asyncio.create_task(agent.respond("Yes"))
        await asyncio.sleep(0.1)
        turn.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            pass
    assert answered(agent.history)
    result = agent.history[-1]["content"]
    assert result["ok"] and result["data"]["status"] == "ESCALATED", "the write finished and its real result was kept"
    ticket = await s.systems.service_ops.get("/tickets/SR0000001")
    assert ticket["status"] == "ESCALATED"


async def test_voice_session_gets_fillers_before_the_answer(api):  # noqa: F811
    sid = (await login(api))["token"]
    assert (await api.post("/api/channel", headers=H(sid), json={"channel": "voice"})).json() == {"channel": "voice"}
    saved = server.FILLER_AFTER_S, server.SLOW_FILLER_AFTER_S
    server.FILLER_AFTER_S, server.SLOW_FILLER_AFTER_S = 0.05, 0.15
    try:
        with slow(mock_systems.loans.app, 300):
            ev = events((await api.post("/api/messages", headers=H(sid), json={"text": "Why did my EMI increase?"})).text)
    finally:
        server.FILLER_AFTER_S, server.SLOW_FILLER_AFTER_S = saved
    kinds = [e for e, _ in ev]
    fillers = [d["text"] for e, d in ev if e == "filler"]
    assert fillers == ["Let me compare your last two EMIs.", "Still checking, thanks for waiting."]
    assert kinds.index("filler") < kinds.index("text")


async def test_quick_lookup_gets_no_filler(api):  # noqa: F811
    sid = (await login(api))["token"]
    await api.post("/api/channel", headers=H(sid), json={"channel": "voice"})
    ev = events((await api.post("/api/messages", headers=H(sid), json={"text": "Why did my EMI increase?"})).text)
    assert "filler" not in [e for e, _ in ev], "the answer beat the filler delay, so nothing should be said first"


async def test_text_session_gets_no_fillers(api):  # noqa: F811
    sid = (await login(api))["token"]
    ev = events((await api.post("/api/messages", headers=H(sid), json={"text": "Why did my EMI increase?"})).text)
    assert "filler" not in [e for e, _ in ev]


async def test_tts_falls_back_without_key_and_metrics_are_stored(api, monkeypatch):  # noqa: F811
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sid = (await login(api))["token"]
    assert (await api.get("/api/health")).json()["tts"] == "browser"
    assert (await api.post("/api/tts", headers=H(sid), json={"text": "Hello"})).status_code == 503
    r = await api.post("/api/voice-metrics", headers=H(sid), json={"transcript_ms": 120, "first_audio_ms": 640})
    assert r.json() == {"ok": True}
    assert await server.state["store"].count("voice_metrics") == 1
    assert (await api.post("/api/tts", headers=H("nope"), json={"text": "Hi"})).status_code == 401
