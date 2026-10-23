"""Web API: login, streamed turns, confirmation flow, session handling. Uses in-process systems and the offline provider."""
import json

import httpx
import pytest

from engine import server
from engine.clients import Systems
from engine.storage import Store
from tests.conftest import RoutingTransport
from tests.offline_llm import OfflineProvider


@pytest.fixture
async def api(tmp_path):
    server.provider_factory = OfflineProvider
    server.systems_factory = lambda: Systems(transport=RoutingTransport())
    server.store_factory = lambda: Store(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://api") as c:
            yield c
    server.provider_factory, server.systems_factory, server.store_factory = None, Systems, Store


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def login(api, phone="+919800000001"):
    r = await api.post("/api/login", json={"phone": phone, "otp": "123456"})
    assert r.status_code == 200
    return r.json()


async def test_login_rejects_wrong_otp_and_unknown_phone(api):
    assert (await api.post("/api/login", json={"phone": "+919800000001", "otp": "000000"})).status_code == 401
    assert (await api.post("/api/login", json={"phone": "+910000000000", "otp": "123456"})).status_code == 404


async def test_login_returns_account_snapshot(api):
    s = await login(api)
    assert s["customer"]["first_name"] == "Priya"
    assert s["account"]["loans"][0]["loan_id"] == "HL1000001"


async def test_message_streams_tools_text_and_done(api):
    sid = (await login(api))["token"]
    r = await api.post("/api/messages", headers=H(sid), json={"text": "Why did my EMI increase?"})
    ev = events(r.text)
    kinds = [e for e, _ in ev]
    assert kinds[0] == "tool" and "tool_result" in kinds and "text" in kinds and kinds[-1] == "done"
    done = ev[-1][1]
    assert "43,298" in done["text"] and "policy:LP-RATE-001 v1" in done["sources"]
    assert done["metrics"]["steps"] == 2 and "loans" in done["backend"]


async def test_action_is_proposed_then_confirmed_in_next_message(api):
    sid = (await login(api, "+919800000003"))["token"]
    first = events((await api.post("/api/messages", headers=H(sid), json={"text": "Please escalate my complaint"})).text)
    action = next(d for e, d in first if e == "action")
    ticket = action["details"]["ticket_id"]
    assert ticket.startswith("SR") and first[-1][1]["pending_actions"] == [action["action_id"]]
    second = events((await api.post("/api/messages", headers=H(sid), json={"text": "Yes, go ahead"})).text)
    assert ticket in second[-1][1]["text"] and second[-1][1]["pending_actions"] == []


async def test_unknown_session_is_rejected(api):
    assert (await api.post("/api/messages", headers=H("nope"), json={"text": "hi"})).status_code == 401
    assert (await api.get("/api/account", headers=H("nope"))).status_code == 401
    assert (await api.get("/api/account")).status_code == 401
