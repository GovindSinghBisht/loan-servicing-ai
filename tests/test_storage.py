"""Conversation storage: persistence through the API, 5-conversation retention, ownership, audit retention."""
from engine import server
from engine.governance import policy
from tests.test_server import H, api, events, login  # noqa: F401  (fixture + helpers)


async def test_messages_tools_and_metrics_are_persisted(api):  # noqa: F811
    sid = (await login(api))["token"]
    await api.post("/api/messages", headers=H(sid), json={"text": "Why did my EMI increase?"})
    convs = (await api.get("/api/conversations", headers=H(sid))).json()["conversations"]
    assert len(convs) == 1 and convs[0]["current"] and convs[0]["turns"] == 1
    assert convs[0]["title"] == "Why did my EMI increase?"
    t = (await api.get(f"/api/conversations/{convs[0]['conversation_id']}", headers=H(sid))).json()
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"]
    assert t["messages"][1]["tools"] == ["explain_emi_change"] and "43,298" in t["messages"][1]["content"]
    store = server.state["store"]
    assert await store.count("turn_metrics") == 1 and await store.count("tool_calls") == 1


async def test_only_latest_conversations_are_kept_per_customer(api):  # noqa: F811
    keep = policy()["retention"]["conversations_per_customer"]
    sids = []
    for i in range(keep + 2):
        sid = (await login(api))["token"]
        await api.post("/api/messages", headers=H(sid), json={"text": f"Question {i}: why did my EMI increase?"})
        sids.append(sid)
    convs = (await api.get("/api/conversations", headers=H(sids[-1]))).json()["conversations"]
    assert len(convs) == keep
    assert {c["title"].split(":")[0] for c in convs} == {f"Question {i}" for i in range(2, keep + 2)}
    store = server.state["store"]
    assert await store.count("messages") == keep * 2, "older conversations' messages were deleted"
    assert await store.count("audit_events", event="login") == keep + 2, "audit outlives chat retention"


async def test_customer_cannot_read_another_customers_conversation(api):  # noqa: F811
    priya = (await login(api, "+919800000001"))["token"]
    await api.post("/api/messages", headers=H(priya), json={"text": "Why did my EMI increase?"})
    conv_id = (await api.get("/api/conversations", headers=H(priya))).json()["conversations"][0]["conversation_id"]
    arjun = (await login(api, "+919800000002"))["token"]
    assert (await api.get(f"/api/conversations/{conv_id}", headers=H(arjun))).status_code == 404
    assert all(c["conversation_id"] != conv_id
               for c in (await api.get("/api/conversations", headers=H(arjun))).json()["conversations"])


async def test_guardrail_events_are_recorded_and_returned(api):  # noqa: F811
    sid = (await login(api))["token"]
    ev = events((await api.post("/api/messages", headers=H(sid), json={"text": "my OTP is 482913"})).text)
    done = ev[-1][1]
    assert done["guardrails"][0]["rule"] == "secret_shared" and "never ask" in done["text"]
    assert await server.state["store"].count("guardrail_events", rule="secret_shared") == 1
    t = (await api.get("/api/conversations", headers=H(sid))).json()["conversations"][0]
    transcript = (await api.get(f"/api/conversations/{t['conversation_id']}", headers=H(sid))).json()
    assert "482913" not in transcript["messages"][0]["content"], "secrets are not stored"


async def test_health_reports_storage_and_tracing(api):  # noqa: F811
    h = (await api.get("/api/health")).json()
    assert h["storage"] == "sqlite" and h["tracing"] in ("off", "langfuse")
