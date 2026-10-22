"""Loan servicing pack. Each tool combines LMS, payments and policy data and does the arithmetic in
shared.finance, returning facts plus the rule checks and sources behind them, so the LLM explains
rather than calculates.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from shared.finance import add_months, prepayment_impact

from ..core import Proposal, Session, ToolError, ToolResult, tool
from .shared import by_id, check, cite, clauses, create_ticket, customer_loans, fy_start, match, owned_loan, r2

PACK = ("loans",)
GST = 0.18


class LoanRef(BaseModel):
    loan_id: str = Field(description="Loan account id, e.g. HL1000001")


async def _today(s: Session) -> date:
    return date.fromisoformat(await s.systems.business_date())


async def _schedule(s: Session, loan_id: str) -> list[dict]:
    return (await s.systems.loans.get(f"/loans/{loan_id}/schedule"))["instalments"]


def _eligible_from(schedule: list[dict], emis_needed: int) -> str | None:
    row = next((r for r in schedule if r["number"] == emis_needed), None)
    return row["due_date"] if row else None


# ---------------------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------------------

@tool("Full details of one loan: rate, EMI, outstanding, dues, reset preference, rate history, NACH mandate.",
      LoanRef, packs=PACK)
async def get_loan_details(s: Session, a: LoanRef) -> ToolResult:
    loan = await owned_loan(s, a.loan_id)
    rh = await s.systems.loans.get(f"/loans/{a.loan_id}/rate-history")
    return ToolResult({**loan, "rate_history": rh["rate_history"]},
                      [f"loans:{a.loan_id}", f"loans:{a.loan_id}/rate-history"])


class EmiChangeArgs(LoanRef):
    month: str | None = Field(None, description="YYYY-MM of the EMI to explain. Omit for the most recent EMI.")


@tool("Explain why an EMI differs from the previous one (rate reset, prepayment, broken-period interest, final "
      "instalment), or why tenure changed while the EMI stayed the same.", EmiChangeArgs, packs=PACK)
async def explain_emi_change(s: Session, a: EmiChangeArgs) -> ToolResult:
    loan = await owned_loan(s, a.loan_id)
    today = await _today(s)
    rows, rh, rate_clauses = await asyncio.gather(
        _schedule(s, a.loan_id), s.systems.loans.get(f"/loans/{a.loan_id}/rate-history"),
        clauses(s, loan["product"], "INTEREST_RATE"))
    if a.month:
        target = next((r for r in rows if r["due_date"].startswith(a.month)), None)
        if not target:
            raise ToolError("NOT_FOUND", f"No EMI falls in {a.month} for {a.loan_id}.")
    else:
        past = [r for r in rows if r["due_date"] <= today.isoformat()]
        target = past[-1] if past else rows[0]
    prev = rows[target["number"] - 2] if target["number"] > 1 else None

    def brief(r):
        return {k: r[k] for k in ("number", "due_date", "rate_pct", "emi", "interest", "principal", "status")}

    reasons, sources = [], [f"loans:{a.loan_id}/schedule#{target['number']}"]
    for e in target["events"]:
        if e["type"] == "RATE_RESET":
            h = next((x for x in rh["rate_history"] if x["effective"] == e["effective"]), {})
            prev_h = next((x for x in reversed(rh["rate_history"]) if x["effective"] < e["effective"]), {})
            reasons.append({**e, "benchmark_from": prev_h.get("eblr_pct"), "benchmark_to": h.get("eblr_pct"),
                            "spread_pct": h.get("spread_pct")})
            sources.append(f"loans:{a.loan_id}/rate-history")
            sources += [cite(c) for c in rate_clauses if c["id"] in ("LP-RATE-001", "LP-RATE-002")]
        else:
            reasons.append(e)
    if prev:
        sources.append(f"loans:{a.loan_id}/schedule#{prev['number']}")
        for e in prev["events"]:
            if e["type"] == "BROKEN_PERIOD_INTEREST":
                reasons.append({"type": "PREVIOUS_EMI_INCLUDED_BROKEN_PERIOD_INTEREST", **e})
    if target["number"] == len(rows) and prev and target["emi"] < prev["emi"]:
        reasons.append({"type": "FINAL_INSTALMENT", "detail": "Last instalment only clears the remaining balance."})
    if not reasons:
        reasons.append({"type": "NO_CHANGE" if prev and prev["emi"] == target["emi"] else "UNEXPLAINED",
                        "detail": "No rate reset, prepayment or other schedule event on this instalment."})

    upcoming = next((r for r in rows if r["due_date"] > today.isoformat()), None)
    return ToolResult({
        "loan_id": a.loan_id, "product": loan["product"], "rate_type": loan["rate_type"],
        "reset_preference": loan["reset_preference"],
        "explained_emi": brief(target), "previous_emi": brief(prev) if prev else None,
        "emi_change": r2(target["emi"] - prev["emi"]) if prev else None,
        "reasons": reasons,
        "next_emi": brief(upcoming) if upcoming else None,
        "remaining_instalments": loan["emis_remaining"], "maturity_date": loan["maturity_date"],
    }, sources)


class PaymentHistoryArgs(LoanRef):
    months: int = Field(6, ge=1, le=36, description="How many months back to look")
    include_successful: bool = Field(False, description="Also list routine successful EMI debits")


@tool("Payment history for a loan: failed debits grouped by instalment with reasons, how each was cleared, the "
      "charges levied or waived and the policy behind them, plus duplicate debits and refunds.",
      PaymentHistoryArgs, packs=PACK)
async def get_payment_history(s: Session, a: PaymentHistoryArgs) -> ToolResult:
    await owned_loan(s, a.loan_id)
    since = add_months(await _today(s), -a.months).isoformat()
    pays, chg = await asyncio.gather(
        s.systems.payments.get(f"/loans/{a.loan_id}/payments", since=since),
        s.systems.loans.get(f"/loans/{a.loan_id}/charges"))
    pays, charges = pays["payments"], chg["charges"]
    emi = [p for p in pays if p["purpose"] == "EMI"]
    failed_insts = sorted({p["instalment_no"] for p in emi if p["status"] == "FAILED"})

    incidents, sources = [], set()
    for n in failed_insts:
        attempts = sorted([p for p in emi if p["instalment_no"] == n], key=lambda p: p["initiated_at"])
        cleared = next((p for p in attempts if p["status"] == "SUCCESS"), None)
        inst_charges = [c for c in charges if c["instalment_no"] == n]
        incidents.append({
            "instalment_no": n, "due_date": attempts[0]["value_date"], "amount": attempts[0]["amount"],
            "attempts": [{k: p[k] for k in ("payment_id", "value_date", "channel", "status", "failure_code",
                                             "failure_reason", "presentation_no")} for p in attempts],
            "outcome": f"cleared on {cleared['value_date']} via {cleared['channel']}" if cleared else "STILL_OVERDUE",
            "charges": [{k: c[k] for k in ("charge_id", "type", "amount", "gst", "total", "status", "policy_ref",
                                           "waiver_reason")} for c in inst_charges],
        })
        sources |= {f"payments:{p['payment_id']}" for p in attempts}
        sources |= {f"policy:{c['policy_ref']}" for c in inst_charges}

    duplicates = [{k: p.get(k) for k in ("payment_id", "value_date", "amount", "duplicate_of", "refund_status",
                                         "dispute_ticket_id")} for p in pays if p.get("duplicate_of")]
    sources |= {f"payments:{d['payment_id']}" for d in duplicates}
    data = {
        "loan_id": a.loan_id, "period_from": since,
        "summary": {
            "emi_debits": len({p["instalment_no"] for p in emi}),
            "instalments_with_failed_debits": len(failed_insts),
            "failed_attempts": sum(p["status"] == "FAILED" for p in emi),
            "charges_paid": r2(sum(c["total"] for i in incidents for c in i["charges"] if c["status"] == "PAID")),
            "charges_unpaid": r2(sum(c["total"] for i in incidents for c in i["charges"] if c["status"] == "UNPAID")),
            "charges_waived": r2(sum(c["total"] for i in incidents for c in i["charges"] if c["status"] == "WAIVED")),
        },
        "failed_debit_incidents": incidents,
        "duplicate_debits": duplicates,
        "part_prepayments": [{k: p[k] for k in ("payment_id", "value_date", "amount")}
                             for p in pays if p["purpose"] == "PART_PREPAYMENT"],
    }
    if a.include_successful:
        data["successful_emi_debits"] = [{k: p[k] for k in ("payment_id", "instalment_no", "value_date", "amount", "channel")}
                                         for p in emi if p["status"] == "SUCCESS" and not p.get("duplicate_of")]
    return ToolResult(data, sorted(sources))


# ---------------------------------------------------------------------------------------------------------
# Eligibility engines (pure functions over fetched data; rules come from policy params)
# ---------------------------------------------------------------------------------------------------------

async def _prepayment_eval(s: Session, loan: dict, amount: float | None) -> dict:
    today = await _today(s)
    fy = fy_start(today)
    cands, rows, fy_pays = await asyncio.gather(
        clauses(s, loan["product"], "PREPAYMENT"), _schedule(s, loan["loan_id"]),
        s.systems.payments.get(f"/loans/{loan['loan_id']}/payments", since=fy.isoformat(), purpose="PART_PREPAYMENT"))
    rule = match([c for c in cands if c["id"] in ("LP-PRE-001", "LP-PRE-002")], loan)
    dues, effect = by_id(cands, "LP-PRE-003"), by_id(cands, "LP-PRE-004")
    if not rule:
        raise ToolError("NO_POLICY", f"No prepayment policy covers {loan['product']} ({loan['rate_type']}).")
    p = rule["params"]
    prepaid = [x for x in fy_pays["payments"] if x["status"] == "SUCCESS"]
    prepaid_amount = sum(x["amount"] for x in prepaid)

    checks = [
        check(rule["id"], loan["emis_paid"] >= p["min_emis_paid"],
              f"{loan['emis_paid']} EMIs paid; minimum {p['min_emis_paid']}"),
        check(dues["id"], loan["overdue_amount"] == 0 and loan["unpaid_charges"] == 0,
              f"overdue Rs {loan['overdue_amount']}, unpaid charges Rs {loan['unpaid_charges']}"),
    ]
    if p.get("max_per_fy"):
        checks.append(check(rule["id"], len(prepaid) < p["max_per_fy"],
                            f"{len(prepaid)} part-prepayments this FY; maximum {p['max_per_fy']}"))
    fee = 0.0
    free_remaining = None
    if amount is not None:
        minimum = p.get("min_amount") or p.get("min_amount_in_emis", 0) * (loan["current_emi"] or 0)
        checks.append(check(rule["id"], amount >= minimum, f"amount Rs {amount:,.0f}; minimum Rs {minimum:,.0f}"))
        checks.append(check("PRINCIPAL", amount < loan["principal_outstanding"],
                            f"principal outstanding Rs {loan['principal_outstanding']:,.2f} (use foreclosure to close fully)"))
        if p.get("free_pct_of_fy_opening_principal"):
            before_fy = [r for r in rows if r["due_date"] < fy.isoformat()]
            fy_open = before_fy[-1]["closing_principal"] if before_fy else loan["sanctioned_amount"]
            free_remaining = max(0.0, fy_open * p["free_pct_of_fy_opening_principal"] / 100 - prepaid_amount)
            fee = max(0.0, amount - free_remaining) * p["charge_pct_on_excess"] / 100
        else:
            fee = amount * p.get("charge_pct", 0) / 100
    eligible = all(c["passed"] for c in checks)
    return {
        "eligible": eligible, "checks": checks, "rule": rule, "effect": effect, "dues": dues,
        "fee": r2(fee), "gst": r2(fee * GST), "free_limit_remaining_this_fy": r2(free_remaining) if free_remaining is not None else None,
        "eligible_from": None if loan["emis_paid"] >= p["min_emis_paid"] else _eligible_from(rows, p["min_emis_paid"]),
        "prepaid_this_fy": {"count": len(prepaid), "amount": r2(prepaid_amount)},
    }


async def _foreclosure_eval(s: Session, loan: dict) -> dict:
    today = await _today(s)
    cands, rows = await asyncio.gather(clauses(s, loan["product"], "FORECLOSURE"), _schedule(s, loan["loan_id"]))
    rule = match([c for c in cands if c["id"] in ("LP-FCL-001", "LP-FCL-002")], loan)
    amount_rule = by_id(cands, "LP-FCL-003")
    if not rule:
        raise ToolError("NO_POLICY", f"No foreclosure policy covers {loan['product']} ({loan['rate_type']}).")
    p = rule["params"]
    paid = loan["emis_paid"]
    checks = [check(rule["id"], paid >= p["min_emis_paid"], f"{paid} EMIs paid; minimum {p['min_emis_paid']}")]
    pct = p.get("charge_pct", 0)
    for slab in p.get("charge_slabs", []):
        if paid >= slab["emis_paid_min"] and (slab["emis_paid_max"] is None or paid <= slab["emis_paid_max"]):
            pct = slab["charge_pct"]
    last_due = max((r["due_date"] for r in rows if r["due_date"] <= today.isoformat()), default=loan["disbursal_date"])
    days = (today - date.fromisoformat(last_due)).days
    principal = loan["principal_outstanding"]
    accrued = principal * loan["current_rate_pct"] / 100 * days / 365
    fee = principal * pct / 100
    total = principal + accrued + loan["overdue_amount"] + loan["unpaid_charges"] + fee * (1 + GST)
    return {
        "eligible": all(c["passed"] for c in checks), "checks": checks, "rule": rule, "amount_rule": amount_rule,
        "eligible_from": None if paid >= p["min_emis_paid"] else _eligible_from(rows, p["min_emis_paid"]),
        "breakdown": {"principal_outstanding": r2(principal),
                      "accrued_interest": {"amount": r2(accrued), "days": days, "since": last_due},
                      "overdue_instalments": loan["overdue_amount"], "unpaid_charges": loan["unpaid_charges"],
                      "foreclosure_charge": {"pct": pct, "amount": r2(fee), "gst": r2(fee * GST)}},
        "total": r2(total),
        "valid_until": (today + timedelta(days=amount_rule["params"]["quote_validity_days"])).isoformat(),
    }


# ---------------------------------------------------------------------------------------------------------
# Quotes and options
# ---------------------------------------------------------------------------------------------------------

class PrepayArgs(LoanRef):
    amount: float = Field(gt=0, description="Prepayment amount in rupees")
    mode: Literal["REDUCE_TENURE", "REDUCE_EMI"] | None = Field(None, description="Omit to compare both")


@tool("Quote a part-prepayment: eligibility checks against policy, charges, and interest saved / tenure or EMI "
      "reduction for each mode. Assumes the current rate holds.", PrepayArgs, packs=PACK)
async def prepayment_quote(s: Session, a: PrepayArgs) -> ToolResult:
    loan = await owned_loan(s, a.loan_id)
    ev = await _prepayment_eval(s, loan, a.amount)
    data = {"loan_id": a.loan_id, "amount": a.amount, "eligible": ev["eligible"], "checks": ev["checks"],
            "eligible_from_if_emis_paid_on_time": ev["eligible_from"],
            "charges": {"fee": ev["fee"], "gst": ev["gst"], "total": r2(ev["fee"] + ev["gst"]),
                        "free_limit_remaining_this_fy": ev["free_limit_remaining_this_fy"]},
            "prepaid_this_fy": ev["prepaid_this_fy"],
            "current": {"principal_outstanding": loan["principal_outstanding"], "rate_pct": loan["current_rate_pct"],
                        "emi": loan["current_emi"], "remaining_instalments": loan["emis_remaining"]},
            "default_mode": ev["effect"]["params"]["default_mode"]}
    if ev["eligible"]:
        impacts = {}
        for mode in ([a.mode] if a.mode else ["REDUCE_TENURE", "REDUCE_EMI"]):
            i = prepayment_impact(loan["principal_outstanding"], loan["current_rate_pct"], loan["current_emi"], a.amount, mode)
            impacts[mode] = {"interest_saved": i.interest_saved, "months_saved": i.months_saved,
                             "new_emi": i.after.emi, "emi_change": i.emi_change, "new_remaining_instalments": i.after.months,
                             "net_saving_after_charges": r2(i.interest_saved - ev["fee"] - ev["gst"])}
        data["impact"] = impacts
        data["assumption"] = ("Floating rate may change in future; savings assume today's rate."
                              if loan["rate_type"] == "FLOATING" else "Fixed rate for the remaining tenure.")
    srcs = [f"loans:{a.loan_id}", cite(ev["rule"]), cite(ev["dues"]), cite(ev["effect"])]
    return ToolResult(data, srcs)


@tool("Foreclosure (full closure) quote: eligibility, itemised amount payable today and quote validity.",
      LoanRef, packs=PACK)
async def foreclosure_quote(s: Session, a: LoanRef) -> ToolResult:
    loan = await owned_loan(s, a.loan_id)
    ev = await _foreclosure_eval(s, loan)
    data = {"loan_id": a.loan_id, "eligible": ev["eligible"], "checks": ev["checks"],
            "eligible_from_if_emis_paid_on_time": ev["eligible_from"]}
    if ev["eligible"]:
        data.update(amount_payable=ev["total"], breakdown=ev["breakdown"], valid_until=ev["valid_until"])
    return ToolResult(data, [f"loans:{a.loan_id}", cite(ev["rule"]), cite(ev["amount_rule"])])


async def _chargeable_bounces(s: Session, loan_id: str, since: str, exclude: str | None = None) -> list[dict]:
    chg = (await s.systems.loans.get(f"/loans/{loan_id}/charges"))["charges"]
    return [c for c in chg if c["type"] == "BOUNCE_CHARGE" and c["status"] != "WAIVED" and c["levied_on"] >= since
            and c["charge_id"] != exclude]


@tool("Everything the customer can do with this loan right now, each with eligibility, cost and the governing "
      "policy: part-prepay, foreclose, top-up, switch reset preference, convert spread.", LoanRef, packs=PACK)
async def get_loan_options(s: Session, a: LoanRef) -> ToolResult:
    loan = await owned_loan(s, a.loan_id)
    today = await _today(s)
    pre, fcl, rate_cl, elig_cl = await asyncio.gather(
        _prepayment_eval(s, loan, None), _foreclosure_eval(s, loan),
        clauses(s, loan["product"], "INTEREST_RATE"), clauses(s, loan["product"], "ELIGIBILITY"))
    pp = pre["rule"]["params"]
    options = [
        {"option": "PART_PREPAYMENT", "available": pre["eligible"], "checks": pre["checks"],
         "cost": "no charge" if pp.get("charge_pct") == 0 else
                 f"{pp['free_pct_of_fy_opening_principal']}% of FY-opening principal free per FY, "
                 f"{pp['charge_pct_on_excess']}% + GST beyond (use prepayment_quote for an exact figure)",
         "eligible_from": pre["eligible_from"], "policy": cite(pre["rule"])},
        {"option": "FORECLOSURE", "available": fcl["eligible"], "checks": fcl["checks"],
         "amount_payable_today": fcl["total"] if fcl["eligible"] else None,
         "charge_pct": fcl["breakdown"]["foreclosure_charge"]["pct"], "eligible_from": fcl["eligible_from"],
         "policy": cite(fcl["rule"])},
    ]
    sources = [f"loans:{a.loan_id}", cite(pre["rule"]), cite(fcl["rule"])]
    if loan["rate_type"] == "FLOATING":
        reset = by_id(rate_cl, "LP-RATE-002")
        conv = by_id(rate_cl, "LP-RATE-003")
        other = "TENURE" if loan["reset_preference"] == "EMI" else "EMI"
        options.append({"option": "SWITCH_RESET_PREFERENCE", "available": True, "current": loan["reset_preference"],
                        "switch_to": other, "cost": "free once per financial year", "policy": cite(reset)})
        no_dues = loan["overdue_amount"] == 0
        fee = loan["principal_outstanding"] * conv["params"]["fee_pct_of_outstanding"] / 100
        options.append({"option": "SPREAD_CONVERSION", "available": no_dues,
                        "checks": [check(conv["id"], no_dues, f"overdue Rs {loan['overdue_amount']}")],
                        "fee": r2(fee), "gst": r2(fee * GST), "policy": cite(conv),
                        "note": "Moves the spread to current new-customer pricing for the same risk grade."})
        sources += [cite(reset), cite(conv)]
    topup = next((c for c in elig_cl if c["id"] == "LP-ELIG-001"), None)
    if topup:
        p = topup["params"]
        bounces = await _chargeable_bounces(s, a.loan_id, add_months(today, -p["no_chargeable_bounce_months"]).isoformat())
        checks = [check(topup["id"], loan["emis_paid"] >= p["min_emis_paid"], f"{loan['emis_paid']} EMIs paid; minimum {p['min_emis_paid']}"),
                  check(topup["id"], not bounces, f"{len(bounces)} customer-side bounce(s) in last {p['no_chargeable_bounce_months']} months"
                        + (f" (latest {bounces[-1]['levied_on']})" if bounces else "")),
                  check(topup["id"], loan["overdue_amount"] == 0, f"overdue Rs {loan['overdue_amount']}")]
        eligible_from = add_months(date.fromisoformat(bounces[-1]["levied_on"]), p["no_chargeable_bounce_months"]).isoformat() if bounces else None
        options.append({"option": "TOP_UP_LOAN", "available": all(c["passed"] for c in checks), "checks": checks,
                        "eligible_from_if_no_further_bounces": eligible_from, "policy": cite(topup),
                        "note": "Amount subject to credit assessment."})
        sources.append(cite(topup))
    return ToolResult({"loan_id": a.loan_id, "options": options}, sources)


class ChargeRef(BaseModel):
    charge_id: str = Field(description="Charge id from payment history, e.g. CHG-0000003")


async def _waiver_eval(s: Session, charge_id: str) -> tuple[dict, dict, dict, list[dict]]:
    today = await _today(s)
    found = None
    for loan in await customer_loans(s):
        chg = (await s.systems.loans.get(f"/loans/{loan['loan_id']}/charges"))["charges"]
        found = next((c for c in chg if c["charge_id"] == charge_id), None)
        if found:
            break
    if not found:
        raise ToolError("NOT_FOUND", f"No charge {charge_id} on this customer's loans.")
    rule_cands, rows, tickets = await asyncio.gather(
        clauses(s, loan["product"], "CHARGES"), _schedule(s, loan["loan_id"]),
        s.systems.service_ops.get(f"/customers/{s.customer_id}/tickets"))
    rule = by_id(rule_cands, "LP-CHG-004")
    p = rule["params"]
    window = add_months(today, -p["window_months"]).isoformat()
    inst = next(r for r in rows if r["number"] == found["instalment_no"])
    others = await _chargeable_bounces(s, loan["loan_id"], window, exclude=charge_id)
    waivers = [t for t in tickets["tickets"] if t["category"] == "BOUNCE_CHARGE_WAIVER"]
    granted = [t for t in waivers if t["created_at"][:10] >= window and (t.get("resolution") or "").startswith("Waiver approved")]
    open_for_this = [t for t in waivers if t["related_entity"].get("id") == charge_id and t["status"] not in ("RESOLVED", "CLOSED")]
    checks = [
        check(rule["id"], found["type"] == "BOUNCE_CHARGE", f"charge type {found['type']}"),
        check(rule["id"], found["status"] != "WAIVED", f"charge status {found['status']}"),
        check(rule["id"], inst["status"] in ("PAID", "PAID_LATE"), f"instalment #{inst['number']} {inst['status']}"),
        check(rule["id"], not others, f"{len(others)} other chargeable bounce(s) since {window}"),
        check(rule["id"], len(granted) < p["max_per_rolling_months"], f"{len(granted)} goodwill waiver(s) granted since {window}"),
        check("DUPLICATE_REQUEST", not open_for_this, f"open waiver request: {open_for_this[0]['ticket_id']}" if open_for_this else "none open"),
    ]
    return found, loan, {"eligible": all(c["passed"] for c in checks), "checks": checks, "rule": rule}, open_for_this


@tool("Check whether a bounce charge can be waived as a goodwill gesture under policy.", ChargeRef, packs=PACK)
async def check_charge_waiver(s: Session, a: ChargeRef) -> ToolResult:
    charge, loan, ev, _ = await _waiver_eval(s, a.charge_id)
    return ToolResult({"charge": {k: charge[k] for k in ("charge_id", "loan_id", "type", "total", "levied_on", "status")},
                       "eligible": ev["eligible"], "checks": ev["checks"]},
                      [f"loans:{loan['loan_id']}/charges/{a.charge_id}", cite(ev["rule"])])


# ---------------------------------------------------------------------------------------------------------
# Actions (each returns a Proposal; executed only after the customer confirms)
# ---------------------------------------------------------------------------------------------------------

@tool("Raise a goodwill waiver request for a bounce charge (checks eligibility first). Requires confirmation.",
      ChargeRef, kind="action", packs=PACK)
async def request_charge_waiver(s: Session, a: ChargeRef) -> Proposal:
    charge, loan, ev, _ = await _waiver_eval(s, a.charge_id)
    if not ev["eligible"]:
        raise ToolError("NOT_ELIGIBLE", "This charge is not eligible for a goodwill waiver.", checks=ev["checks"])
    return Proposal(
        summary=f"Raise a waiver request for the Rs {charge['total']:,.0f} bounce charge on loan {loan['loan_id']} "
                f"(levied {charge['levied_on']}). Loan servicing decides within 5 working days.",
        details={"charge_id": a.charge_id, "loan_id": loan["loan_id"], "amount": charge["total"]},
        run=lambda: create_ticket(s, "BOUNCE_CHARGE_WAIVER", {"type": "CHARGE", "id": a.charge_id, "loan_id": loan["loan_id"]},
                            f"Goodwill waiver requested for bounce charge {a.charge_id} (Rs {charge['total']}).",
                            {"policy": cite(ev["rule"]), "checks": ev["checks"]}, f"{s.session_id}:waiver:{a.charge_id}"),
    )


class PrepayRequestArgs(LoanRef):
    amount: float = Field(gt=0)
    mode: Literal["REDUCE_TENURE", "REDUCE_EMI"] = "REDUCE_TENURE"


@tool("Initiate a part-prepayment request with the quoted terms (checks eligibility first). Requires confirmation.",
      PrepayRequestArgs, kind="action", packs=PACK)
async def request_prepayment(s: Session, a: PrepayRequestArgs) -> Proposal:
    loan = await owned_loan(s, a.loan_id)
    ev = await _prepayment_eval(s, loan, a.amount)
    if not ev["eligible"]:
        raise ToolError("NOT_ELIGIBLE", "Part-prepayment is not allowed on this loan right now.", checks=ev["checks"])
    i = prepayment_impact(loan["principal_outstanding"], loan["current_rate_pct"], loan["current_emi"], a.amount, a.mode)
    terms = {"loan_id": a.loan_id, "amount": a.amount, "mode": a.mode, "charges": r2(ev["fee"] + ev["gst"]),
             "interest_saved": i.interest_saved, "months_saved": i.months_saved, "new_emi": i.after.emi}
    return Proposal(
        summary=f"Part-prepay Rs {a.amount:,.0f} on {a.loan_id} ({a.mode.replace('_', ' ').lower()}), charges "
                f"Rs {terms['charges']:,.0f}. A payment link will be sent to the registered mobile.",
        details=terms,
        run=lambda: create_ticket(s, "PREPAYMENT_REQUEST", {"type": "LOAN", "id": a.loan_id},
                            f"Part-prepayment of Rs {a.amount:,.0f} ({a.mode}).", terms,
                            f"{s.session_id}:prepay:{a.loan_id}:{a.amount}:{a.mode}"),
    )


class DisputeArgs(BaseModel):
    payment_id: str = Field(description="The debit being disputed, e.g. PAY-0000085")
    reason: str = Field(description="What went wrong, in the customer's words")


@tool("Raise a payment dispute, e.g. a duplicate auto-debit (within 30 days of the debit; returns the existing "
      "request if one is already open). Requires confirmation.", DisputeArgs, kind="action", packs=PACK)
async def raise_payment_dispute(s: Session, a: DisputeArgs) -> Proposal | ToolResult:
    pay = await s.systems.payments.get(f"/payments/{a.payment_id}")
    loan = await owned_loan(s, pay["loan_id"])
    today = await _today(s)
    rule = by_id(await clauses(s, loan["product"], "SERVICE"), "LP-SVC-001")
    tickets = (await s.systems.service_ops.get(f"/customers/{s.customer_id}/tickets"))["tickets"]
    existing = next((t for t in tickets if t["category"] == "PAYMENT_DISPUTE" and t["status"] not in ("RESOLVED", "CLOSED")
                     and a.payment_id in (t["related_entity"].get("payment_id"), t["related_entity"].get("id"))), None)
    if existing:
        return ToolResult({"already_raised": True, **{k: existing[k] for k in ("ticket_id", "status", "created_at",
                                                                                "sla_due_on", "sla_breached")}},
                          [f"service_ops:{existing['ticket_id']}"])
    age = (today - date.fromisoformat(pay["value_date"])).days
    if age > rule["params"]["raise_within_days"]:
        raise ToolError("OUTSIDE_WINDOW", f"Disputes must be raised within {rule['params']['raise_within_days']} days; "
                                          f"this debit was {age} days ago.", policy=cite(rule))
    return Proposal(
        summary=f"Raise a dispute for the Rs {pay['amount']:,.0f} debit on {pay['value_date']} ({a.payment_id}). "
                f"Confirmed duplicates are refunded within {rule['params']['refund_within_working_days']} working days.",
        details={"payment_id": a.payment_id, "loan_id": loan["loan_id"], "amount": pay["amount"], "reason": a.reason},
        run=lambda: create_ticket(s, "PAYMENT_DISPUTE", {"type": "LOAN", "id": loan["loan_id"], "payment_id": a.payment_id},
                            a.reason, {"policy": cite(rule)}, f"{s.session_id}:dispute:{a.payment_id}"),
    )
