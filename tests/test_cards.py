"""Credit card pack: golden facts for the card journeys, and pack composition on the shared engine."""
from engine.core import execute, tools_for
from engine.packs import compose, packs_for


async def test_late_fee_explained_from_failed_autopay_and_slab(session):
    r = await execute(session("CUST100002"), "explain_card_charges", {"card_id": "CC1000001"})
    fee = r["data"]["late_fee"]
    assert fee["failed_payment_attempts"][0]["failure_code"] == "INSUFFICIENT_FUNDS"
    assert (fee["payment_due_date"], fee["paid_on"], fee["days_after_due"], fee["grace_days"]) == ("2026-10-02", "2026-10-06", 4, 3)
    assert (fee["fee"], fee["total"]) == (950, 1121.0)  # Rs 10,001-25,000 slab on a Rs 19,059.61 statement
    assert "policy:CP-FEE-001 v1" in r["sources"] and "policy:CP-AP-001 v1" in r["sources"]


async def test_late_fee_reversal_proposed_then_confirmed(session):
    s = session("CUST100002")
    assert (await execute(s, "check_late_fee_reversal", {"card_id": "CC1000001"}))["data"]["eligible"]
    proposal = await execute(s, "request_late_fee_reversal", {"card_id": "CC1000001"})
    assert proposal["status"] == "CONFIRMATION_REQUIRED" and proposal["details"]["amount"] == 1121.0
    s.begin_turn()
    done = await execute(s, "confirm_action", {"action_id": proposal["action_id"]})
    assert done["data"]["category"] == "LATE_FEE_REVERSAL"
    again = await execute(s, "check_late_fee_reversal", {"card_id": "CC1000001"})
    assert not again["data"]["eligible"], "an open request blocks a duplicate"


async def test_existing_card_dispute_is_returned_not_duplicated(session):
    s = session("CUST100003")
    txns = (await execute(s, "get_card_transactions", {"card_id": "CC1000002", "disputed_only": True}))["data"]["transactions"]
    r = await execute(s, "raise_card_dispute", {"txn_id": txns[0]["txn_id"], "reason": "not mine"})
    d = r["data"]
    assert d["already_disputed"] and d["ticket_id"] == "SR0000003" and d["temporary_credit_due_by"] == "2026-10-25"


async def test_block_card_needs_confirmation_and_blocks(session):
    s = session("CUST100003")
    proposal = await execute(s, "block_card", {"card_id": "CC1000002", "reason": "lost my wallet"})
    assert proposal["status"] == "CONFIRMATION_REQUIRED"
    card = await s.systems.cards.get("/cards/CC1000002")
    assert card["status"] == "ACTIVE", "nothing happens before confirmation"
    s.begin_turn()
    done = await execute(s, "confirm_action", {"action_id": proposal["action_id"]})
    assert done["data"]["status"] == "BLOCKED"
    assert (await execute(s, "block_card", {"card_id": "CC1000002", "reason": "again"}))["error"] == "ALREADY_BLOCKED"


async def test_customer_cannot_see_another_customers_card(session):
    r = await execute(session("CUST100001"), "get_card_details", {"card_id": "CC1000001"})
    assert r["error"] == "NOT_FOUND"


def test_packs_compose_over_shared_core():
    core = {t.name for t in tools_for({"core"})}
    loans = {t.name for t in tools_for(set(compose(["loans"]).tool_groups))}
    cards = {t.name for t in tools_for(set(compose(["cards"]).tool_groups))}
    both = {t.name for t in tools_for(set(compose(["loans", "cards"]).tool_groups))}
    assert core <= loans and core <= cards and both == loans | cards
    assert "confirm_action" in core and "block_card" in cards - loans


async def test_packs_follow_what_the_customer_holds(session):
    async def packs(cid):
        return packs_for((await execute(session(cid), "get_customer_overview", {}))["data"])
    assert await packs("CUST100001") == ["loans"]
    assert await packs("CUST100002") == ["loans", "cards"]
