"""Credit cards, statements and transactions: the second workflow (pack #2) that reuses the platform.

Fees follow data/policies/card_policies.json (CP-FEE-001 late fee slabs, CP-INT-001 finance charges).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from shared.finance import add_months

from .loans import Ids
from .reference import AS_OF, FAILURE_REASONS, GST_PCT, MERCHANTS

STATEMENT_DAY = 12
DUE_AFTER_DAYS = 20
LATE_GRACE_DAYS = 3          # CP-FEE-001
FINANCE_PCT_PER_MONTH = 3.75  # CP-INT-001
LATE_FEE_SLABS = [(500, 0), (5000, 500), (10000, 750), (25000, 950), (50000, 1100), (None, 1300)]
N_STATEMENTS = 6


def late_fee(total_due: float) -> int:
    return next(fee for cap, fee in LATE_FEE_SLABS if cap is None or total_due <= cap)


@dataclass
class StatementScript:
    """Override how one statement is paid. kind: FULL | MIN | LATE | AUTOPAY_FAILED"""
    kind: str
    days_after_due: int = 0
    code: str | None = None


@dataclass
class ScriptedTxn:
    txn_date: date
    merchant: str
    category: str
    amount: float
    dispute: dict | None = None


@dataclass
class CardSpec:
    card_id: str
    customer_id: str
    product: str
    network: str
    last4: str
    credit_limit: int
    autopay_mode: str | None  # FULL | MIN | None
    behaviour: str            # TRANSACTOR | REVOLVER
    spend_level: float        # multiplier on transaction count
    bank: str
    account_last4: str
    late_probability: float = 0.04  # chance of an unscripted late payment per statement
    scripted_statements: dict[tuple[int, int], StatementScript] = field(default_factory=dict)
    scripted_txns: list[ScriptedTxn] = field(default_factory=list)


def _statement_dates() -> list[date]:
    last = date(AS_OF.year, AS_OF.month, STATEMENT_DAY)
    if last > AS_OF:
        last = add_months(last, -1)
    return [add_months(last, -i) for i in range(N_STATEMENTS, -1, -1)]  # includes the opening boundary


def build_card(spec: CardSpec, ids: Ids, rng: random.Random) -> tuple[dict, list[dict], list[dict]]:
    """Returns (card, statements, transactions)."""
    bounds = _statement_dates()
    txns: list[dict] = []
    statements: list[dict] = []

    def txn(on: date, ttype: str, amount: float, merchant: str, category: str, **extra) -> dict:
        t = {"txn_id": ids.next("TXN", 8), "card_id": spec.card_id, "txn_date": on.isoformat(),
             "type": ttype, "merchant": merchant, "category": category, "amount": round(amount, 2),
             "status": "POSTED", "dispute": None}
        t.update(extra)
        txns.append(t)
        return t

    # Purchases across all cycles, including the current unbilled one.
    day = bounds[0] + timedelta(days=1)
    while day <= AS_OF:
        if rng.random() < 0.45 * spec.spend_level:
            name, cat, lo, hi = rng.choice(MERCHANTS)
            txn(day, "PURCHASE", lo + (hi - lo) * rng.random() ** 2, name, cat)  # skewed towards smaller spends
        day += timedelta(days=1)
    for s in spec.scripted_txns:
        txn(s.txn_date, "PURCHASE", s.amount, s.merchant, s.category,
            dispute={**s.dispute, "dispute_id": ids.next("DSP", 6)} if s.dispute else None)

    carried = 0.0
    pending_fee = 0.0
    for i in range(1, len(bounds)):
        start, end = bounds[i - 1] + timedelta(days=1), bounds[i]
        in_cycle = [t for t in txns if t["type"] == "PURCHASE" and start.isoformat() <= t["txn_date"] <= end.isoformat()]
        purchases = round(sum(t["amount"] for t in in_cycle if not (t["dispute"] and t["dispute"]["status"] == "RESOLVED_REVERSED")), 2)
        finance = round(carried * FINANCE_PCT_PER_MONTH / 100, 2) if carried > 0 else 0.0
        if finance:
            txn(end, "FINANCE_CHARGE", finance, "Finance charges", "FEE", policy_ref="CP-INT-001")
        if pending_fee:
            txn(end, "LATE_FEE", pending_fee, "Late payment fee", "FEE", policy_ref="CP-FEE-001")
            txn(end, "GST", round(pending_fee * GST_PCT / 100, 2), "GST on fees", "FEE")
        fees = round(pending_fee * (1 + GST_PCT / 100), 2)
        total = round(carried + purchases + finance + fees, 2)
        min_due = total if total <= 200 else max(200.0, round(total * 0.05, 2))
        due = end + timedelta(days=DUE_AFTER_DAYS)

        st = {
            "statement_id": ids.next("STM", 6), "card_id": spec.card_id, "statement_date": end.isoformat(),
            "period_start": start.isoformat(), "period_end": end.isoformat(), "due_date": due.isoformat(),
            "previous_balance": round(carried, 2), "purchases": purchases, "finance_charges": finance,
            "fees_and_gst": fees, "total_due": total, "min_due": round(min_due, 2),
            "amount_paid": 0.0, "paid_on": None, "payment_status": None, "payment_attempts": [],
        }
        pending_fee = 0.0
        script = spec.scripted_statements.get((end.year, end.month))
        kind = script.kind if script else (
            "LATE" if rng.random() < spec.late_probability else ("FULL" if spec.behaviour == "TRANSACTOR" else "MIN"))

        if due > AS_OF:
            st["payment_status"] = "NOT_YET_DUE"
            statements.append(st)
            carried = 0.0
            continue

        if kind == "AUTOPAY_FAILED":
            st["payment_attempts"].append({"on": due.isoformat(), "channel": "AUTOPAY", "amount": total,
                                           "status": "FAILED", "failure_code": script.code,
                                           "failure_reason": FAILURE_REASONS.get(script.code)})
        paid_amt = min_due if kind == "MIN" else total
        paid_on = due + timedelta(days=script.days_after_due if script else (rng.randint(4, 12) if kind == "LATE" else 0))
        channel = "AUTOPAY" if kind in ("FULL", "MIN") and spec.autopay_mode else "UPI"
        if paid_on <= AS_OF and total > 0:
            st["payment_attempts"].append({"on": paid_on.isoformat(), "channel": channel, "amount": round(paid_amt, 2),
                                           "status": "SUCCESS", "failure_code": None, "failure_reason": None})
            txn(paid_on, "PAYMENT", -paid_amt, "Payment received - thank you", "PAYMENT", channel=channel)
            st.update(amount_paid=round(paid_amt, 2), paid_on=paid_on.isoformat())
        days_late = (paid_on - due).days
        if total <= 0:
            st["payment_status"] = "NO_DUES"
        elif days_late > LATE_GRACE_DAYS:
            st["payment_status"] = "PAID_LATE" if paid_on <= AS_OF else "OVERDUE"
            pending_fee = late_fee(total)
        else:
            st["payment_status"] = "PAID_FULL" if paid_amt >= total else "PAID_MIN"
        carried = round(total - st["amount_paid"], 2)
        statements.append(st)

    unbilled = round(sum(t["amount"] for t in txns if t["txn_date"] > bounds[-1].isoformat() and t["type"] == "PURCHASE"), 2)
    last = statements[-1]
    outstanding = round(last["total_due"] - last["amount_paid"] + unbilled, 2)
    txns.sort(key=lambda t: (t["txn_date"], t["txn_id"]))
    card = {
        "card_id": spec.card_id, "customer_id": spec.customer_id, "product": spec.product,
        "network": spec.network, "card_number_masked": f"XXXX XXXX XXXX {spec.last4}", "status": "ACTIVE",
        "credit_limit": spec.credit_limit, "outstanding": outstanding,
        "available_limit": round(max(spec.credit_limit - outstanding, 0), 2), "unbilled_amount": unbilled,
        "statement_day": STATEMENT_DAY, "autopay": {"enabled": spec.autopay_mode is not None, "mode": spec.autopay_mode,
                                                   "bank": spec.bank, "account": f"XXXXXX{spec.account_last4}"},
        "reward_points": rng.randint(200, 25000),
    }
    return card, statements, txns
