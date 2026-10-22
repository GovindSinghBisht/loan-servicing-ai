"""Loan accounts, repayment schedules, payment attempts and charges.

The schedule comes from shared.finance; payment outcomes are simulated per instalment and
charges are levied according to data/policies/loan_policies.json (LP-CHG-*).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from typing import Any

from shared.finance import PartPrepayment, RateChange, ResetMode, build_schedule

from .reference import AS_OF, EBLR_HISTORY, FAILURE_REASONS, FAILURE_WEIGHTS, GST_PCT, TECHNICAL_FAILURES

BOUNCE_V2_FROM = date(2025, 4, 1)  # LP-CHG-001 v2: Rs 500 once per instalment; v1: Rs 400 per presentation
LPI_PCT_PER_MONTH = 2              # LP-CHG-003
REPRESENT_AFTER_DAYS = 5
MAX_TENURE_MONTHS = {"HOME_LOAN": 360, "LAP": 180}  # LP-RATE-002


@dataclass
class Outcome:
    """How one instalment was collected. `days` = days after due date when it was cleared."""
    kind: str  # ON_TIME | REPRESENT_OK | CUSTOMER_PAID | UNPAID | DUPLICATE
    code: str | None = None
    days: int = 0
    channel: str = "UPI"


@dataclass
class LoanSpec:
    loan_id: str
    customer_id: str
    product: str  # HOME_LOAN | LAP | PERSONAL_LOAN
    principal: float
    tenure_months: int
    disbursal_date: date
    first_due_date: date
    rate_type: str  # FIXED | FLOATING
    fixed_rate: float | None = None
    spread: float | None = None
    spread_changes: list[tuple[date, float, str]] = field(default_factory=list)
    reset_mode: ResetMode = "EMI"
    prepayments: list[PartPrepayment] = field(default_factory=list)
    scripted: dict[tuple[int, int], Outcome] = field(default_factory=dict)  # keyed by (year, month) of due date
    failure_rate: float = 0.0
    bank: str = "HDFC Bank"
    account_last4: str = "0000"


class Ids:
    def __init__(self):
        self._n: dict[str, int] = {}

    def next(self, prefix: str, width: int = 7) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}-{self._n[prefix]:0{width}d}"


def _at(values: list[tuple[date, Any]], d: date) -> Any:
    return [v for eff, v in values if eff <= d][-1]


def rate_events(spec: LoanSpec) -> list[dict]:
    """Rate history for the loan: sanction rate plus every EBLR or spread change after disbursal."""
    if spec.rate_type == "FIXED":
        return [{"effective": spec.disbursal_date, "rate_pct": spec.fixed_rate, "eblr_pct": None,
                 "spread_pct": None, "reason": "Fixed rate at sanction"}]
    spreads = [(spec.disbursal_date, spec.spread)] + [(d, s) for d, s, _ in spec.spread_changes]
    spread_reasons = {d: r for d, _, r in spec.spread_changes}
    dates = sorted({spec.disbursal_date} | {d for d, _ in EBLR_HISTORY if d > spec.disbursal_date} | set(spread_reasons))
    events = []
    for d in dates:
        eblr, spread = _at(EBLR_HISTORY, d), _at(spreads, d)
        if d == spec.disbursal_date:
            reason = f"Rate at sanction: EBLR {eblr}% + spread {spread}%"
        elif d in spread_reasons:
            reason = spread_reasons[d]
        else:
            reason = f"EBLR revised to {eblr}%"
        events.append({"effective": d, "rate_pct": round(eblr + spread, 2), "eblr_pct": eblr,
                       "spread_pct": spread, "reason": reason})
    return events


def random_outcome(rng: random.Random, failure_rate: float) -> Outcome:
    if rng.random() >= failure_rate:
        return Outcome("ON_TIME")
    code = rng.choices(list(FAILURE_WEIGHTS), weights=list(FAILURE_WEIGHTS.values()))[0]
    if code in TECHNICAL_FAILURES:
        return Outcome("REPRESENT_OK", code, 3)
    if rng.random() < 0.55:
        return Outcome("REPRESENT_OK", code, REPRESENT_AFTER_DAYS)
    return Outcome("CUSTOMER_PAID", code, rng.randint(6, 20), rng.choice(["UPI", "NETBANKING", "BRANCH_CASH"]))


def _ts(d: date, rng: random.Random, channel: str) -> str:
    hour = rng.randint(6, 9) if channel == "NACH" else rng.randint(9, 22)
    return f"{d.isoformat()}T{hour:02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}+05:30"


def build_loan(spec: LoanSpec, ids: Ids, rng: random.Random) -> tuple[dict, list[dict], list[dict]]:
    """Returns (loan account with schedule, payments, charges)."""
    events = rate_events(spec)
    schedule = build_schedule(
        spec.principal, spec.tenure_months, spec.disbursal_date, spec.first_due_date,
        [RateChange(e["effective"], e["rate_pct"], e["reason"]) for e in events],
        spec.reset_mode, spec.prepayments, MAX_TENURE_MONTHS.get(spec.product),
    )
    payments: list[dict] = []
    charges: list[dict] = []
    account = f"XXXXXX{spec.account_last4}"

    def payment(inst_no, purpose, channel, amount, on, status, code=None, presentation=None, **extra):
        p = {
            "payment_id": ids.next("PAY"), "loan_id": spec.loan_id, "customer_id": spec.customer_id,
            "instalment_no": inst_no, "purpose": purpose, "channel": channel, "amount": round(amount, 2),
            "initiated_at": _ts(on, rng, channel), "value_date": on.isoformat(), "status": status,
            "failure_code": code, "failure_reason": FAILURE_REASONS.get(code) if code else None,
            "presentation_no": presentation, "source_bank": spec.bank, "source_account": account,
            "utr": f"UTR{rng.randint(10**11, 10**12 - 1)}" if status == "SUCCESS" else None,
        }
        p.update(extra)
        payments.append(p)
        return p

    def charge(inst_no, ctype, amount, levied_on, policy_ref, gst=True, **extra):
        c = {
            "charge_id": ids.next("CHG"), "loan_id": spec.loan_id, "instalment_no": inst_no, "type": ctype,
            "amount": round(amount, 2), "gst": round(amount * GST_PCT / 100, 2) if gst else 0.0,
            "levied_on": levied_on.isoformat(), "policy_ref": policy_ref,
            "status": "UNPAID", "paid_on": None, "waiver_reason": None,
        }
        c["total"] = round(c["amount"] + c["gst"], 2)
        c.update(extra)
        charges.append(c)
        return c

    for pp in spec.prepayments:
        payment(None, "PART_PREPAYMENT", "NETBANKING", pp.amount, pp.on, "SUCCESS", prepayment_mode=pp.mode)

    rows = []
    next_due_marked = False
    for inst in schedule:
        row = inst.to_dict()
        row.update(status=None, paid_on=None, days_late=0)
        due = inst.due_date
        if due > AS_OF:
            row["status"] = "UPCOMING" if next_due_marked else "DUE"
            row["projected"] = spec.rate_type == "FLOATING"  # future floating rates are not known yet
            next_due_marked = True
            rows.append(row)
            continue

        o = spec.scripted.get((due.year, due.month)) or random_outcome(rng, spec.failure_rate)
        if o.kind in ("ON_TIME", "DUPLICATE"):
            payment(inst.number, "EMI", "NACH", inst.emi, due, "SUCCESS", presentation=1)
            if o.kind == "DUPLICATE":
                payment(inst.number, "EMI", "NACH", inst.emi, due, "SUCCESS", presentation=1,
                        duplicate_of=payments[-1]["payment_id"], refund_status="PENDING")
            row.update(status="PAID", paid_on=due.isoformat())
            rows.append(row)
            continue

        failed_on = [due]
        payment(inst.number, "EMI", "NACH", inst.emi, due, "FAILED", o.code, presentation=1)
        cleared = due + timedelta(days=o.days)
        cleared_ok = False
        if o.kind == "REPRESENT_OK":
            if cleared <= AS_OF:
                payment(inst.number, "EMI", "NACH", inst.emi, cleared, "SUCCESS", presentation=2)
                cleared_ok = True
            else:
                row["next_presentation_on"] = cleared.isoformat()
        else:  # CUSTOMER_PAID or UNPAID: re-presentation also bounces
            rep = due + timedelta(days=REPRESENT_AFTER_DAYS)
            if rep <= AS_OF:
                payment(inst.number, "EMI", "NACH", inst.emi, rep, "FAILED", o.code, presentation=2)
                failed_on.append(rep)
            if o.kind == "CUSTOMER_PAID" and cleared <= AS_OF:
                payment(inst.number, "EMI", o.channel, inst.emi, cleared, "SUCCESS")
                cleared_ok = True

        technical = o.code in TECHNICAL_FAILURES
        if cleared_ok:
            row.update(status="PAID_LATE", paid_on=cleared.isoformat(), days_late=o.days)
        else:
            row.update(status="OVERDUE", days_late=(AS_OF - due).days)
        rows.append(row)

        # Charges are collected with the next EMI debit if that has happened by AS_OF.
        collect_on = due + timedelta(days=31) if cleared_ok and due + timedelta(days=31) <= AS_OF else None

        if technical:
            charge(inst.number, "BOUNCE_CHARGE", 500 if due >= BOUNCE_V2_FROM else 400, due, "LP-CHG-002",
                   status="WAIVED", waiver_reason="Technical failure at bank/network; not customer-caused")
            continue
        bounce_dates = failed_on[:1] if due >= BOUNCE_V2_FROM else failed_on
        for d in bounce_dates:
            ref, amt = ("LP-CHG-001 v2", 500) if due >= BOUNCE_V2_FROM else ("LP-CHG-001 v1", 400)
            c = charge(inst.number, "BOUNCE_CHARGE", amt, d, ref)
            if collect_on:
                c.update(status="PAID", paid_on=collect_on.isoformat())
        days_late = o.days if cleared_ok else (AS_OF - due).days
        lpi = inst.emi * LPI_PCT_PER_MONTH * 12 / 100 * days_late / 365
        c = charge(inst.number, "LATE_PAYMENT_INTEREST", lpi, cleared if cleared_ok else AS_OF, "LP-CHG-003",
                   gst=False, days=days_late, accruing=not cleared_ok)
        if collect_on:
            c.update(status="PAID", paid_on=collect_on.isoformat())

    past = [r for r in rows if r["status"] not in ("DUE", "UPCOMING")]
    upcoming = [r for r in rows if r["status"] in ("DUE", "UPCOMING")]
    overdue = [r for r in past if r["status"] == "OVERDUE"]
    unpaid_charges = [c for c in charges if c["status"] == "UNPAID"]
    closed = not upcoming and not overdue
    last_past = past[-1] if past else None

    loan = {
        "loan_id": spec.loan_id, "customer_id": spec.customer_id, "product": spec.product,
        "borrower_type": "INDIVIDUAL", "status": "CLOSED" if closed else "ACTIVE",
        "sanctioned_amount": spec.principal, "disbursal_date": spec.disbursal_date.isoformat(),
        "original_tenure_months": spec.tenure_months, "rate_type": spec.rate_type,
        "reset_preference": spec.reset_mode if spec.rate_type == "FLOATING" else None,
        "current_rate_pct": (upcoming or past)[0 if upcoming else -1]["rate_pct"],
        "rate_history": [{**e, "effective": e["effective"].isoformat()} for e in events],
        "current_emi": upcoming[0]["emi"] if upcoming else None,
        "next_due_date": upcoming[0]["due_date"] if upcoming else None,
        "next_due_amount": upcoming[0]["emi"] if upcoming else None,
        "principal_outstanding": last_past["closing_principal"] if last_past else spec.principal,
        "emis_paid": sum(r["status"] in ("PAID", "PAID_LATE") for r in past),
        "emis_remaining": len(upcoming),
        "overdue_instalments": len(overdue),
        "overdue_amount": round(sum(r["emi"] for r in overdue), 2),
        "unpaid_charges": round(sum(c["total"] for c in unpaid_charges), 2),
        "maturity_date": rows[-1]["due_date"],
        "closed_on": rows[-1]["due_date"] if closed else None,
        "nach_mandate": {"umrn": f"UMRN{rng.randint(10**11, 10**12 - 1)}", "bank": spec.bank,
                         "account": account, "status": "ACTIVE" if not closed else "CANCELLED",
                         "debit_day": spec.first_due_date.day},
        "schedule": rows,
    }
    return loan, payments, charges
