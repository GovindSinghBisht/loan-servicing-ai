"""Check generated data for internal consistency and that each demo persona's story still holds.

    python -m datagen.validate [--data data/generated]

Exits non-zero on any failure, so it can gate CI or a pre-demo check.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from .loans import MAX_TENURE_MONTHS
from .reference import AS_OF, GST_PCT

ROOT = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def load(d: Path) -> dict:
    return {f.stem: json.loads(f.read_text()) for f in d.glob("*.json")}


def check_integrity(db: dict) -> None:
    customers = {c["customer_id"] for c in db["crm"]["customers"]}
    loans = {l["loan_id"]: l for l in db["loans"]["loans"]}
    cards = {c["card_id"] for c in db["cards"]["cards"]}
    payments = db["payments"]["payments"]
    today = AS_OF.isoformat()

    for name, rows, key in [("customer", db["crm"]["customers"], "customer_id"), ("loan", db["loans"]["loans"], "loan_id"),
                            ("payment", payments, "payment_id"), ("charge", db["loans"]["charges"], "charge_id"),
                            ("ticket", db["service_ops"]["tickets"], "ticket_id"), ("txn", db["cards"]["transactions"], "txn_id")]:
        dupes = [k for k, n in Counter(r[key] for r in rows).items() if n > 1]
        check(not dupes, f"duplicate {name} ids: {dupes[:5]}")

    prepaid = Counter()
    for p in payments:
        check(p["loan_id"] in loans, f"{p['payment_id']} references unknown loan")
        check(p["value_date"] <= today, f"{p['payment_id']} dated in the future")
        if p["purpose"] == "PART_PREPAYMENT":
            prepaid[p["loan_id"]] += p["amount"]

    emi_success = Counter((p["loan_id"], p["instalment_no"]) for p in payments
                          if p["purpose"] == "EMI" and p["status"] == "SUCCESS" and not p.get("duplicate_of"))
    for l in loans.values():
        check(l["customer_id"] in customers, f"{l['loan_id']} references unknown customer")
        s = l["schedule"]
        check(abs(s[-1]["closing_principal"]) < 0.01, f"{l['loan_id']} does not amortize to zero")
        repaid = sum(r["principal"] for r in s) + prepaid[l["loan_id"]]
        check(abs(repaid - l["sanctioned_amount"]) < 1, f"{l['loan_id']} principal repaid {repaid:.2f} != sanctioned")
        for prev, cur in zip(s, s[1:]):
            check(cur["number"] == prev["number"] + 1, f"{l['loan_id']} instalment numbering gap")
            check(cur["opening_principal"] <= prev["closing_principal"] + 0.01, f"{l['loan_id']} balance jumps at #{cur['number']}")
        if l["product"] in MAX_TENURE_MONTHS:
            check(len(s) <= MAX_TENURE_MONTHS[l["product"]], f"{l['loan_id']} tenure {len(s)} exceeds product max")
        for r in s:
            n = emi_success[(l["loan_id"], r["number"])]
            if r["status"] in ("PAID", "PAID_LATE"):
                check(n == 1, f"{l['loan_id']} #{r['number']} {r['status']} but {n} successful EMI payments")
            elif r["status"] == "OVERDUE":
                check(n == 0, f"{l['loan_id']} #{r['number']} overdue but has a successful payment")
            check(r["status"] in ("DUE", "UPCOMING") or r["due_date"] <= today, f"{l['loan_id']} #{r['number']} status/date mismatch")

    for c in db["loans"]["charges"]:
        check(c["loan_id"] in loans, f"{c['charge_id']} references unknown loan")
        check(abs(c["total"] - c["amount"] - c["gst"]) < 0.01, f"{c['charge_id']} total != amount + gst")
        if c["type"] == "BOUNCE_CHARGE":
            check(abs(c["gst"] - c["amount"] * GST_PCT / 100) < 0.01, f"{c['charge_id']} wrong GST")

    for t in db["service_ops"]["tickets"]:
        check(t["customer_id"] in customers, f"{t['ticket_id']} references unknown customer")
        ent = t["related_entity"]
        if ent["type"] == "LOAN":
            check(ent["id"] in loans, f"{t['ticket_id']} references unknown loan")
        if ent["type"] == "CARD":
            check(ent["id"] in cards, f"{t['ticket_id']} references unknown card")

    for i in db["crm"]["interactions"]:
        check(i["customer_id"] in customers, f"{i['interaction_id']} references unknown customer")
        check(i["at"][:10] <= today, f"{i['interaction_id']} dated in the future")

    for st in db["cards"]["statements"]:
        expected = st["previous_balance"] + st["purchases"] + st["finance_charges"] + st["fees_and_gst"]
        check(abs(st["total_due"] - expected) < 0.02, f"{st['statement_id']} total_due does not add up")
    for t in db["cards"]["transactions"]:
        check(t["card_id"] in cards, f"{t['txn_id']} references unknown card")
        check(t["txn_date"] <= today, f"{t['txn_id']} dated in the future")


def check_personas(db: dict) -> None:
    loans = {l["loan_id"]: l for l in db["loans"]["loans"]}
    payments = db["payments"]["payments"]
    charges = db["loans"]["charges"]
    tickets = {t["ticket_id"]: t for t in db["service_ops"]["tickets"]}
    row = lambda lid, due: next(r for r in loans[lid]["schedule"] if r["due_date"] == due)
    six_months_ago = date(2026, 4, 22).isoformat()

    # Priya: EMI rose in October because of the EBLR hike; 2 failed EMIs in 6 months, one waived as bank-side.
    sep, oct_ = row("HL1000001", "2026-09-05"), row("HL1000001", "2026-10-05")
    check(oct_["emi"] > sep["emi"] and any(e["type"] == "RATE_RESET" for e in oct_["events"]), "Priya: Oct EMI should rise on rate reset")
    fails = [p for p in payments if p["loan_id"] == "HL1000001" and p["status"] == "FAILED" and p["value_date"] >= six_months_ago]
    check(len(fails) == 2, f"Priya: expected 2 failed payments in last 6 months, got {len(fails)}")
    waived = [c for c in charges if c["loan_id"] == "HL1000001" and c["status"] == "WAIVED"]
    check(len(waived) == 1 and waived[0]["policy_ref"] == "LP-CHG-002", "Priya: Aug bounce should be waived (bank-side)")
    check(loans["HL1000001"]["overdue_amount"] == 0, "Priya: should have no overdue")

    # Arjun: fewer than 12 EMIs paid and overdue, so foreclosure is blocked twice over; card late fee after autopay failure.
    arjun = loans["PL1000002"]
    check(arjun["emis_paid"] < 12 and arjun["overdue_amount"] > 0, "Arjun: should be in lock-in and overdue")
    fees = [t for t in db["cards"]["transactions"] if t["card_id"] == "CC1000001" and t["type"] == "LATE_FEE"]
    check(len(fees) == 1 and fees[0]["txn_date"] == "2026-10-12", f"Arjun: expected one late fee on 2026-10-12, got {fees}")

    # Kavya: rate hike extended tenure, EMI unchanged; duplicate debit pending refund, SLA breached; open card dispute.
    sep, oct_ = row("LP1000003", "2026-09-05"), row("LP1000003", "2026-10-05")
    check(oct_["emi"] == sep["emi"] and any("remaining_months_to" in e for e in oct_["events"]), "Kavya: Oct reset should change tenure, not EMI")
    dup = [p for p in payments if p["loan_id"] == "LP1000003" and p.get("duplicate_of")]
    check(len(dup) == 1 and dup[0]["refund_status"] == "PENDING", "Kavya: expected one duplicate debit pending refund")
    check(tickets["SR0000001"]["sla_breached"], "Kavya: SR0000001 should have breached SLA")
    disputed = [t for t in db["cards"]["transactions"] if t["card_id"] == "CC1000002" and t["dispute"]]
    check(len(disputed) == 1 and disputed[0]["dispute"]["status"] == "OPEN", "Kavya: expected one open card dispute")

    # Rohan: two active loans; personal loan past 24 EMIs (2% foreclosure slab, part-prepayment allowed).
    mine = [l for l in loans.values() if l["customer_id"] == "CUST100004" and l["status"] == "ACTIVE"]
    check(len(mine) == 2, "Rohan: expected two active loans")
    check(loans["PL1000005"]["emis_paid"] > 24 and loans["PL1000005"]["overdue_amount"] == 0, "Rohan: PL should be >24 EMIs, no overdue")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "generated")
    db = load(ap.parse_args().data)
    check_integrity(db)
    check_personas(db)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures[:50]:
            print("  -", f)
        sys.exit(1)
    print(f"OK: {len(db['loans']['loans'])} loans, {len(db['payments']['payments'])} payments, "
          f"{len(db['cards']['transactions'])} card transactions; all persona stories hold")


if __name__ == "__main__":
    main()
