"""Login sessions: hashed bearer tokens, sign-out revocation, restore after restart, idle and absolute expiry."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from engine import server
from engine.storage import auth_sessions
from tests.test_server import H, api, login  # noqa: F401  (fixture + helpers)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _row(token: str) -> dict:
    return await server.state["store"].get_auth_session(_hash(token))


async def test_login_stores_only_a_hash_of_the_token(api):  # noqa: F811
    s = await login(api)
    row = await _row(s["token"])
    assert row["customer_id"] == "CUST100001" and row["revoked_at"] is None
    assert row["token_hash"] != s["token"] and s["token"] not in str(row)
    assert (await api.get("/api/me", headers=H(s["token"]))).json()["customer"]["first_name"] == "Priya"


async def test_logout_revokes_the_token_immediately(api):  # noqa: F811
    token = (await login(api))["token"]
    assert (await api.post("/api/logout", headers=H(token))).json() == {"ok": True}
    assert (await api.get("/api/me", headers=H(token))).status_code == 401
    row = await _row(token)
    assert row["revoked_reason"] == "logout", "revoked sessions are kept for audit"


async def test_session_and_conversation_survive_a_restart(api):  # noqa: F811
    token = (await login(api))["token"]
    await api.post("/api/messages", headers=H(token), json={"text": "Why did my EMI increase?"})
    conv_before = (await api.get("/api/conversations", headers=H(token))).json()["conversations"][0]["conversation_id"]
    server.state["sessions"].clear()  # what a restart (or a request landing on another instance) looks like

    me = (await api.get("/api/me", headers=H(token))).json()
    assert me["customer"]["first_name"] == "Priya" and me["turns"] == 1
    live = server.state["sessions"][_hash(token)]
    assert live.agent.session.session_id == conv_before
    assert any(m.get("content") == "Why did my EMI increase?" for m in live.agent.history), "history restored"

    await api.post("/api/messages", headers=H(token), json={"text": "Show payments that failed"})
    convs = (await api.get("/api/conversations", headers=H(token))).json()["conversations"]
    assert [c["turns"] for c in convs if c["current"]] == [2], "the same conversation continues"


async def test_idle_session_expires_and_is_revoked(api):  # noqa: F811
    token = (await login(api))["token"]
    server.state["sessions"][_hash(token)].last_used -= 31 * 60
    assert (await api.get("/api/me", headers=H(token))).status_code == 401
    assert (await _row(token))["revoked_reason"] == "idle_timeout"


async def test_expired_session_is_not_restored_after_restart(api):  # noqa: F811
    token = (await login(api))["token"]
    async with server.state["store"].engine.begin() as conn:
        await conn.execute(update(auth_sessions).where(auth_sessions.c.token_hash == _hash(token))
                           .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    server.state["sessions"].clear()
    assert (await api.get("/api/me", headers=H(token))).status_code == 401


async def test_revoked_session_cannot_be_revived_by_a_restart(api):  # noqa: F811
    token = (await login(api))["token"]
    await api.post("/api/logout", headers=H(token))
    server.state["sessions"].clear()
    assert (await api.get("/api/me", headers=H(token))).status_code == 401


async def test_missing_or_malformed_token_is_rejected(api):  # noqa: F811
    assert (await api.get("/api/me")).status_code == 401
    assert (await api.get("/api/me", headers={"Authorization": "Basic abc"})).status_code == 401
