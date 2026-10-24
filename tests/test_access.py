"""Public-demo protection: the optional access code and the per-minute rate limits."""
from engine.governance import policy
from tests.test_server import H, api, login  # noqa: F401  (fixture + helpers)

PRIYA = {"phone": "+919800000001", "otp": "123456"}


async def test_access_code_is_required_when_configured(api, monkeypatch):  # noqa: F811
    assert (await api.get("/api/health")).json()["access_code_required"] is False
    monkeypatch.setenv("DEMO_ACCESS_CODE", "blue-42")
    assert (await api.get("/api/health")).json()["access_code_required"] is True
    assert (await api.post("/api/login", json=PRIYA)).status_code == 403
    assert (await api.post("/api/login", json={**PRIYA, "access_code": "wrong"})).status_code == 403
    assert (await api.post("/api/login", json={**PRIYA, "access_code": " blue-42 "})).status_code == 200


async def test_sign_ins_are_rate_limited_per_ip(api):  # noqa: F811
    limit = policy()["rate_limits"]["login_per_ip_per_minute"]
    codes = [(await api.post("/api/login", json={**PRIYA, "otp": "000000"})).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [401] * limit, "wrong OTPs count toward the limit"
    r = await api.post("/api/login", json=PRIYA)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_messages_are_rate_limited_per_session(api):  # noqa: F811
    token = (await login(api))["token"]
    limit = policy()["rate_limits"]["messages_per_session_per_minute"]
    for _ in range(limit):
        assert (await api.post("/api/messages", headers=H(token), json={"text": "hi"})).status_code == 200
    r = await api.post("/api/messages", headers=H(token), json={"text": "hi"})
    assert r.status_code == 429 and "wait" in r.json()["detail"]
