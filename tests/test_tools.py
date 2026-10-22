"""Golden facts at the tool layer: the numbers the assistant must say for each demo journey."""
from engine.core import execute


async def test_priya_emi_increase_is_explained_by_rate_reset(session):
    r = await execute(session("CUST100001"), "explain_emi_change", {"loan_id": "HL1000001"})
    d = r["data"]
    assert d["previous_emi"]["emi"] == 42088 and d["explained_emi"]["emi"] == 43298 and d["emi_change"] == 1210
    reset = d["reasons"][0]
    assert reset["type"] == "RATE_RESET" and (reset["from_rate"], reset["to_rate"]) == (9.5, 10.0)
    assert (reset["benchmark_from"], reset["benchmark_to"]) == (8.5, 9.0)
    assert "policy:LP-RATE-001 v1" in r["sources"]


async def test_priya_failed_payments_last_six_months(session):
    d = (await execute(session("CUST100001"), "get_payment_history", {"loan_id": "HL1000001", "months": 6}))["data"]
    incidents = d["failed_debit_incidents"]
    assert [i["attempts"][0]["failure_code"] for i in incidents] == ["INSUFFICIENT_FUNDS", "BANK_TECHNICAL_ERROR"]
    assert incidents[1]["charges"][0]["status"] == "WAIVED"
    assert d["summary"]["charges_waived"] == 590


async def test_priya_prepayment_has_no_charge_and_saves_interest(session):
    d = (await execute(session("CUST100001"), "prepayment_quote", {"loan_id": "HL1000001", "amount": 50000}))["data"]
    assert d["eligible"] and d["charges"]["total"] == 0
    assert d["impact"]["REDUCE_TENURE"]["interest_saved"] == 161934.46
    assert d["impact"]["REDUCE_TENURE"]["months_saved"] == 4
    assert d["impact"]["REDUCE_TENURE"]["interest_saved"] > d["impact"]["REDUCE_EMI"]["interest_saved"]


async def test_priya_topup_blocked_by_recent_bounce_but_waiver_allowed(session):
    s = session("CUST100001")
    opts = {o["option"]: o for o in (await execute(s, "get_loan_options", {"loan_id": "HL1000001"}))["data"]["options"]}
    assert not opts["TOP_UP_LOAN"]["available"] and opts["PART_PREPAYMENT"]["available"]
    assert (await execute(s, "check_charge_waiver", {"charge_id": "CHG-0000003"}))["data"]["eligible"]


async def test_arjun_prepayment_and_foreclosure_blocked(session):
    s = session("CUST100002")
    pre = (await execute(s, "prepayment_quote", {"loan_id": "PL1000002", "amount": 50000}))["data"]
    failed = {c["rule"] for c in pre["checks"] if not c["passed"]}
    assert not pre["eligible"] and failed == {"LP-PRE-002", "LP-PRE-003"}
    fcl = (await execute(s, "foreclosure_quote", {"loan_id": "PL1000002"}))["data"]
    assert not fcl["eligible"] and fcl["eligible_from_if_emis_paid_on_time"] == "2027-02-05"


async def test_kavya_rate_hike_extended_tenure(session):
    d = (await execute(session("CUST100003"), "explain_emi_change", {"loan_id": "LP1000003"}))["data"]
    assert d["emi_change"] == 0
    assert (d["reasons"][0]["remaining_months_from"], d["reasons"][0]["remaining_months_to"]) == (106, 110)


async def test_kavya_duplicate_dispute_returns_existing_ticket(session):
    r = await execute(session("CUST100003"), "raise_payment_dispute", {"payment_id": "PAY-0000085", "reason": "twice"})
    assert r["data"]["already_raised"] and r["data"]["ticket_id"] == "SR0000001" and r["data"]["sla_breached"]


async def test_rohan_personal_loan_quotes(session):
    s = session("CUST100004")
    pre = (await execute(s, "prepayment_quote", {"loan_id": "PL1000005", "amount": 100000, "mode": "REDUCE_TENURE"}))["data"]
    assert pre["eligible"] and pre["charges"]["total"] == 0 and pre["charges"]["free_limit_remaining_this_fy"] == 112614.25
    fcl = (await execute(s, "foreclosure_quote", {"loan_id": "PL1000005"}))["data"]
    assert fcl["breakdown"]["foreclosure_charge"]["pct"] == 2


async def test_customer_cannot_read_another_customers_loan(session):
    r = await execute(session("CUST100001"), "get_loan_details", {"loan_id": "PL1000002"})
    assert r == {"ok": False, "error": "NOT_FOUND", "message": "No loan PL1000002 on this customer's profile."}


async def test_actions_need_confirmation_in_a_later_turn(session):
    s = session("CUST100003")
    proposal = await execute(s, "escalate_service_request", {"ticket_id": "SR0000001", "reason": "refund overdue"})
    assert proposal["status"] == "CONFIRMATION_REQUIRED"
    same_turn = await execute(s, "confirm_action", {"action_id": proposal["action_id"]})
    assert same_turn["error"] == "CONFIRMATION_REQUIRED"
    s.begin_turn()
    done = await execute(s, "confirm_action", {"action_id": proposal["action_id"]})
    assert done["ok"] and done["data"]["status"] == "ESCALATED"
    again = await execute(s, "confirm_action", {"action_id": proposal["action_id"]})
    assert again["error"] == "NO_SUCH_ACTION"
