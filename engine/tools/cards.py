"""Credit card servicing pack: the second workflow on the shared platform.

Everything below is card-specific logic. What it reuses unchanged: resilient clients, customer scoping
and ownership checks, versioned policy lookup, rule checks with sources, the proposal/confirmation gate,
ticket creation, audit, the agent loop, the web and voice channels.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from pydantic import BaseModel, Field

from ..clients import NotFound
from ..core import Proposal, Session, ToolError, ToolResult, tool
from .shared import by_id, check, cite, clauses, create_ticket, propose_next, r2

PACK = ("cards",)
PRODUCT = "CREDIT_CARD"
GST = 0.18


class CardRef(BaseModel):
    card_id: str = Field(description="Card id from the customer snapshot, e.g. CC1000001")


async def _owned_card(s: Session, card_id: str) -> dict:
    try:
        card = await s.systems.cards.get(f"/cards/{card_id}")
    except NotFound:
        card = None
    if not card or card["customer_id"] != s.customer_id:
        raise ToolError("NOT_FOUND", f"No card {card_id} on this customer's profile.")
    return card


async def _today(s: Session) -> date:
    return date.fromisoformat(await s.systems.business_date())


def _late_fee(slabs: list[dict], total_due: float) -> int:
    return next(sl["fee"] for sl in slabs if sl["total_due_max"] is None or total_due <= sl["total_due_max"])


@tool("Card summary: limit, outstanding, available limit, unbilled spend, autopay, status, and the latest "
      "statement (total due, minimum due, due date, payment status).", CardRef, packs=PACK)
async def get_card_details(s: Session, a: CardRef) -> ToolResult:
    card = await _owned_card(s, a.card_id)
    sts = (await s.systems.cards.get(f"/cards/{a.card_id}/statements", limit=1))["statements"]
    latest = sts[-1] if sts else None
    return ToolResult({**card, "latest_statement": latest},
                      [f"cards:{a.card_id}"] + ([f"cards:{a.card_id}/statement/{latest['statement_date']}"] if latest else []))


class ChargesArgs(CardRef):
    statement_month: str | None = Field(None, description="YYYY-MM of the statement to explain; omit for the most "
                                                          "recent statement that has fees or interest")


@tool("Explain fees and interest on a card statement: which payment was late or failed and why, the policy slab "
      "that set the late fee, GST, and finance charges on any carried balance.", ChargesArgs, packs=PACK)
async def explain_card_charges(s: Session, a: ChargesArgs) -> ToolResult:
    await _owned_card(s, a.card_id)
    sts, fee_cl, int_cl, ap_cl = await asyncio.gather(
        s.systems.cards.get(f"/cards/{a.card_id}/statements", limit=12),
        clauses(s, PRODUCT, "CHARGES"), clauses(s, PRODUCT, "INTEREST"), clauses(s, PRODUCT, "PAYMENTS"))
    sts = sts["statements"]
    if a.statement_month:
        target = next((x for x in sts if x["statement_date"].startswith(a.statement_month)), None)
    else:
        target = next((x for x in reversed(sts) if x["fees_and_gst"] or x["finance_charges"]), None)
    if not target:
        return ToolResult({"card_id": a.card_id, "charges_found": False,
                           "detail": "No late fees or finance charges on recent statements."}, [f"cards:{a.card_id}"])

    fee_rule, int_rule, autopay_rule = by_id(fee_cl, "CP-FEE-001"), by_id(int_cl, "CP-INT-001"), by_id(ap_cl, "CP-AP-001")
    idx = sts.index(target)
    prev = sts[idx - 1] if idx > 0 else None
    sources = [f"cards:{a.card_id}/statement/{target['statement_date']}"]
    out: dict = {"card_id": a.card_id, "charges_found": True,
                 "statement": {k: target[k] for k in ("statement_date", "due_date", "total_due", "min_due",
                                                      "fees_and_gst", "finance_charges", "previous_balance", "purchases")}}
    if target["fees_and_gst"] and prev:
        p = fee_rule["params"]
        paid_on = date.fromisoformat(prev["paid_on"]) if prev["paid_on"] else None
        due = date.fromisoformat(prev["due_date"])
        fee = _late_fee(p["slabs"], prev["total_due"])
        failed = [x for x in prev["payment_attempts"] if x["status"] == "FAILED"]
        out["late_fee"] = {
            "for_statement_dated": prev["statement_date"], "payment_due_date": prev["due_date"],
            "total_due_on_that_statement": prev["total_due"], "paid_on": prev["paid_on"],
            "days_after_due": (paid_on - due).days if paid_on else None, "grace_days": p["grace_days_after_due"],
            "failed_payment_attempts": failed, "fee": fee, "gst": r2(fee * GST), "total": r2(fee * (1 + GST)),
            "slab_rule": "fee set by the total amount due on the late statement",
        }
        sources += [f"cards:{a.card_id}/statement/{prev['statement_date']}", cite(fee_rule)]
        if failed:
            out["autopay_note"] = autopay_rule["text"]
            sources.append(cite(autopay_rule))
    if target["finance_charges"]:
        out["finance_charges"] = {"amount": target["finance_charges"], "carried_balance": target["previous_balance"],
                                  "rate_pct_per_month": int_rule["params"]["pct_per_month"]}
        sources.append(cite(int_rule))
    return ToolResult(out, sources)


class TxnArgs(CardRef):
    days: int = Field(60, ge=1, le=180, description="How many days back")
    category: str | None = Field(None, description="e.g. FUEL, GROCERY, ONLINE_SHOPPING, TRAVEL")
    disputed_only: bool = False


@tool("Card transactions over a period, newest first, with totals by category and any disputes.", TxnArgs, packs=PACK)
async def get_card_transactions(s: Session, a: TxnArgs) -> ToolResult:
    await _owned_card(s, a.card_id)
    since = (await _today(s) - timedelta(days=a.days)).isoformat()
    rows = (await s.systems.cards.get(f"/cards/{a.card_id}/transactions", since=since,
                                      disputed=True if a.disputed_only else None))["transactions"]
    purchases = [t for t in rows if t["type"] == "PURCHASE" and (not a.category or t["category"] == a.category)]
    by_cat: dict[str, float] = {}
    for t in purchases:
        by_cat[t["category"]] = by_cat.get(t["category"], 0) + t["amount"]
    shown = sorted(purchases if not a.disputed_only else rows, key=lambda t: t["txn_date"], reverse=True)[:25]
    return ToolResult({
        "card_id": a.card_id, "since": since, "purchase_count": len(purchases),
        "purchase_total": r2(sum(t["amount"] for t in purchases)),
        "by_category": {k: r2(v) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
        "transactions": [{k: t[k] for k in ("txn_id", "txn_date", "merchant", "category", "amount", "type", "dispute")}
                         for t in shown],
        "fees_and_payments": [{k: t[k] for k in ("txn_date", "type", "merchant", "amount")}
                              for t in rows if t["type"] != "PURCHASE"][-10:],
    }, [f"cards:{a.card_id}/transactions"])


async def _reversal_eval(s: Session, card_id: str) -> tuple[dict, dict, dict]:
    await _owned_card(s, card_id)
    today = await _today(s)
    sts, cl, tickets = await asyncio.gather(
        s.systems.cards.get(f"/cards/{card_id}/statements", limit=12), clauses(s, PRODUCT, "CHARGES"),
        s.systems.service_ops.get(f"/customers/{s.customer_id}/tickets"))
    rule = by_id(cl, "CP-FEE-002")
    p = rule["params"]
    sts = sts["statements"]
    late = next((x for x in reversed(sts) if x["payment_status"] == "PAID_LATE"), None)
    if not late:
        raise ToolError("NO_LATE_FEE", "There is no late payment fee on this card to reverse.")
    window = (today - timedelta(days=30 * p["window_months"])).isoformat()
    reversals = [t for t in tickets["tickets"] if t["category"] == "LATE_FEE_REVERSAL"]
    granted = [t for t in reversals if t["created_at"][:10] >= window and t["status"] in ("RESOLVED", "CLOSED")
               and "approved" in (t.get("resolution") or "").lower()]
    open_for_card = [t for t in reversals if t["related_entity"].get("id") == card_id and t["status"] not in ("RESOLVED", "CLOSED")]
    days_late = (date.fromisoformat(late["paid_on"]) - date.fromisoformat(late["due_date"])).days
    fee_statement = sts[sts.index(late) + 1] if sts.index(late) + 1 < len(sts) else None
    checks = [
        check(rule["id"], days_late <= p["paid_within_days_of_due"],
              f"paid {days_late} days after the {late['due_date']} due date; limit {p['paid_within_days_of_due']}"),
        check(rule["id"], len(granted) < p["max_per_rolling_months"], f"{len(granted)} reversal(s) granted since {window}"),
        check("DUPLICATE_REQUEST", not open_for_card,
              f"open request {open_for_card[0]['ticket_id']}" if open_for_card else "none open"),
    ]
    fee = fee_statement["fees_and_gst"] if fee_statement else None
    return ({"eligible": all(c["passed"] for c in checks), "checks": checks, "fee_with_gst": fee,
             "late_statement": late["statement_date"]}, rule, {"fee_statement": fee_statement})


@tool("Check whether a card late payment fee could be reversed as a one-time goodwill gesture. Only for \"could I\" "
      "questions: when the customer asks for the reversal, call request_late_fee_reversal instead (it checks too).",
      CardRef, packs=PACK)
async def check_late_fee_reversal(s: Session, a: CardRef) -> ToolResult:
    ev, rule, _ = await _reversal_eval(s, a.card_id)
    data = {"card_id": a.card_id, **ev}
    if ev["eligible"]:
        data["next_step"] = propose_next("request_late_fee_reversal")
    return ToolResult(data, [f"cards:{a.card_id}", cite(rule)])


@tool("Request a goodwill reversal of a card late fee (checks eligibility first). Requires confirmation.",
      CardRef, kind="action", packs=PACK)
async def request_late_fee_reversal(s: Session, a: CardRef) -> Proposal:
    ev, rule, _ = await _reversal_eval(s, a.card_id)
    if not ev["eligible"]:
        raise ToolError("NOT_ELIGIBLE", "This late fee is not eligible for a goodwill reversal.", checks=ev["checks"])
    return Proposal(
        summary=f"Request reversal of the Rs {ev['fee_with_gst']:,.0f} late fee (including GST) on card {a.card_id}. "
                f"Card servicing decides within 5 working days and credits it to the next statement.",
        details={"card_id": a.card_id, "amount": ev["fee_with_gst"], "late_statement": ev["late_statement"]},
        run=lambda: create_ticket(s, "LATE_FEE_REVERSAL", {"type": "CARD", "id": a.card_id},
                                  f"Goodwill reversal of late fee (Rs {ev['fee_with_gst']}).",
                                  {"policy": cite(rule), "checks": ev["checks"]}, f"{s.session_id}:lfr:{a.card_id}"),
    )


class DisputeArgs(BaseModel):
    txn_id: str = Field(description="Transaction id from the card transactions list")
    reason: str = Field(description="What is wrong, in the customer's words")


@tool("Dispute a card transaction the customer doesn't recognise or has a problem with (within 60 days). Returns "
      "the existing dispute if one is already open. Requires confirmation.", DisputeArgs, kind="action", packs=PACK)
async def raise_card_dispute(s: Session, a: DisputeArgs) -> Proposal | ToolResult:
    today = await _today(s)
    cards = (await s.systems.cards.get(f"/customers/{s.customer_id}/cards"))["cards"]
    txn, card = None, None
    for c in cards:
        rows = (await s.systems.cards.get(f"/cards/{c['card_id']}/transactions"))["transactions"]
        txn = next((t for t in rows if t["txn_id"] == a.txn_id), None)
        if txn:
            card = c
            break
    if not txn or not card:
        raise ToolError("NOT_FOUND", f"No transaction {a.txn_id} on this customer's cards.")
    rule = by_id(await clauses(s, PRODUCT, "DISPUTES"), "CP-DSP-001")
    p = rule["params"]
    if txn["dispute"]:
        return ToolResult({"already_disputed": True, **txn["dispute"],
                           "temporary_credit_due_by": (date.fromisoformat(txn["dispute"]["raised_on"])
                                                       + timedelta(days=p["temporary_credit_after_days"])).isoformat(),
                           "resolution_due_by": (date.fromisoformat(txn["dispute"]["raised_on"])
                                                 + timedelta(days=p["resolve_within_days"])).isoformat()},
                          [f"cards:{card['card_id']}/transactions/{a.txn_id}", cite(rule)])
    age = (today - date.fromisoformat(txn["txn_date"])).days
    if age > p["raise_within_days"]:
        raise ToolError("OUTSIDE_WINDOW", f"Card disputes must be raised within {p['raise_within_days']} days; this "
                                          f"transaction was {age} days ago.", policy=cite(rule))
    return Proposal(
        summary=f"Dispute the Rs {txn['amount']:,.0f} transaction at {txn['merchant']} on {txn['txn_date']}. If it isn't "
                f"resolved within {p['temporary_credit_after_days']} days you get a temporary credit; resolution within "
                f"{p['resolve_within_days']} days.",
        details={"txn_id": a.txn_id, "card_id": card["card_id"], "amount": txn["amount"], "merchant": txn["merchant"]},
        run=lambda: create_ticket(s, "CARD_DISPUTE", {"type": "CARD", "id": card["card_id"], "txn_id": a.txn_id},
                                  a.reason, {"policy": cite(rule)}, f"{s.session_id}:cdsp:{a.txn_id}"),
    )


class BlockArgs(CardRef):
    reason: str = Field(description="Why, e.g. lost, stolen, suspicious transactions")


@tool("Block a card immediately (lost, stolen or misuse). Requires confirmation; cannot be undone in this channel.",
      BlockArgs, kind="action", packs=PACK)
async def block_card(s: Session, a: BlockArgs) -> Proposal:
    card = await _owned_card(s, a.card_id)
    if card["status"] == "BLOCKED":
        raise ToolError("ALREADY_BLOCKED", f"Card {card['card_number_masked']} is already blocked.")
    rule = by_id(await clauses(s, PRODUCT, "SECURITY"), "CP-BLK-001")

    async def run() -> ToolResult:
        out = await s.systems.cards.post(f"/cards/{a.card_id}/block", {"reason": a.reason},
                                         idempotency_key=f"{s.session_id}:block:{a.card_id}")
        return ToolResult({"card_id": a.card_id, "status": out["status"], "blocked_at": out.get("blocked_at"),
                           "zero_liability_if_reported_within_working_days":
                               rule["params"]["zero_liability_report_within_working_days"]},
                          [f"cards:{a.card_id}", cite(rule)])

    return Proposal(
        summary=f"Block card {card['card_number_masked']} ({card['product']}) now. It can't be used after this; a "
                f"replacement can be requested separately.",
        details={"card_id": a.card_id, "reason": a.reason},
        run=run,
    )
