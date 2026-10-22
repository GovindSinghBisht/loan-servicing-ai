"""Generate synthetic data for the mock enterprise systems.

    python -m datagen.generate [--customers 200] [--out data/generated]

Output is deterministic for a given seed. One file per backend system, so each mock service owns its data:
crm.json, loans.json, payments.json, service_ops.json, cards.json (+ manifest.json, demo_personas.json).
Policies are hand-authored in data/policies/ and served by the Policy Knowledge service.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from shared.finance import PartPrepayment, add_months

from . import personas
from .cards import CardSpec, build_card
from .loans import Ids, LoanSpec, build_loan
from .reference import (AS_OF, BANKS, CITIES, FIRST_NAMES, LANGUAGES, LAST_NAMES, RISK_PROFILES, SEED)

ROOT = Path(__file__).resolve().parent.parent

TICKET_CATEGORIES = {  # SLA in working days (0 = immediate), owning team
    "PREPAYMENT_REQUEST": (3, "LOAN_SERVICING"),
    "FORECLOSURE_QUOTE": (2, "LOAN_SERVICING"),
    "PAYMENT_DISPUTE": (7, "PAYMENTS_OPS"),
    "BOUNCE_CHARGE_WAIVER": (5, "LOAN_SERVICING"),
    "STATEMENT_REQUEST": (1, "LOAN_SERVICING"),
    "NACH_MANDATE_CHANGE": (10, "PAYMENTS_OPS"),
    "RESET_PREFERENCE_CHANGE": (5, "LOAN_SERVICING"),
    "CARD_DISPUTE": (30, "CARD_DISPUTES"),
    "LATE_FEE_REVERSAL": (5, "CARD_SERVICING"),
    "CARD_BLOCK": (0, "CARD_SERVICING"),
    "GENERAL_QUERY": (3, "CONTACT_CENTER"),
}


def add_working_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def first_due_after(disbursal: date) -> date:
    """EMI cycle on the 5th; first EMI in the month after the next if disbursed after the 20th."""
    nxt = add_months(date(disbursal.year, disbursal.month, 5), 1)
    return add_months(nxt, 1) if disbursal.day > 20 else nxt


def random_customer(n: int, rng: random.Random) -> dict:
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    city, state, pin = rng.choice(CITIES)
    age = rng.randint(24, 60)
    risk = rng.choices(list(RISK_PROFILES), weights=[w for w, _ in RISK_PROFILES.values()])[0]
    return {
        "customer_id": f"CUST{100100 + n}", "first_name": first, "last_name": last,
        "dob": date(AS_OF.year - age, rng.randint(1, 12), rng.randint(1, 28)).isoformat(),
        "gender": "F" if first in FIRST_NAMES[20:] else "M",
        "phone": f"+91-98{rng.randint(10**7, 10**8 - 1)}", "email": f"{first.lower()}.{last.lower()}{n}@example.com",
        "city": city, "state": state, "pincode": f"{pin}{rng.randint(1, 99):03d}",
        "preferred_language": rng.choice(["en", "en", LANGUAGES[city]]),
        "segment": rng.choices(["MASS", "AFFLUENT"], weights=[75, 25])[0], "customer_since": None,
        "risk_profile": risk,
    }


def random_loans(cust: dict, ids: Ids, rng: random.Random) -> list[LoanSpec]:
    failure_rate = RISK_PROFILES[cust["risk_profile"]][1]
    roll = rng.random()
    products = (["PERSONAL_LOAN"] if roll < 0.5 else ["HOME_LOAN"] if roll < 0.75 else ["LAP"] if roll < 0.9
                else ["HOME_LOAN", "PERSONAL_LOAN"])
    bank, last4 = rng.choice(BANKS), f"{rng.randint(0, 9999):04d}"
    specs = []
    for product in products:
        if product == "PERSONAL_LOAN":
            months_ago, tenure = rng.randint(2, 50), rng.choice([12, 24, 36, 48, 60])
            principal, kw = rng.randrange(100_000, 1_500_001, 25_000), {"rate_type": "FIXED", "fixed_rate": rng.choice([11.25, 12.5, 13.75, 14.5, 15.99, 17.5])}
        elif product == "HOME_LOAN":
            months_ago, tenure = rng.randint(3, 52), rng.choice([180, 240, 300])
            principal, kw = rng.randrange(1_500_000, 8_000_001, 100_000), {"rate_type": "FLOATING", "spread": rng.choice([0.5, 0.75, 1.0, 1.5, 2.0])}
        else:
            months_ago, tenure = rng.randint(3, 52), rng.choice([84, 120, 144])
            principal, kw = rng.randrange(1_000_000, 6_000_001, 100_000), {"rate_type": "FLOATING", "spread": rng.choice([2.0, 2.5, 3.0, 3.5])}
        disbursal = add_months(AS_OF, -months_ago).replace(day=rng.randint(1, 28))
        disbursal = max(disbursal, date(2022, 4, 15))
        spec = LoanSpec(
            loan_id=ids.next({"PERSONAL_LOAN": "PL", "HOME_LOAN": "HL", "LAP": "LP"}[product], 7).replace("-", ""),
            customer_id=cust["customer_id"], product=product, principal=float(principal), tenure_months=tenure,
            disbursal_date=disbursal, first_due_date=first_due_after(disbursal),
            reset_mode=rng.choice(["EMI", "TENURE"]) if kw["rate_type"] == "FLOATING" else "EMI",
            failure_rate=failure_rate, bank=bank, account_last4=last4, **kw,
        )
        if kw["rate_type"] == "FLOATING" and months_ago > 18 and rng.random() < 0.15:
            on = add_months(disbursal, rng.randint(13, months_ago - 2)).replace(day=rng.randint(6, 28))
            spec.prepayments.append(PartPrepayment(on, round(principal * rng.uniform(0.05, 0.15), -4),
                                                   rng.choice(["REDUCE_EMI", "REDUCE_TENURE"])))
        if kw["rate_type"] == "FLOATING" and months_ago > 12 and rng.random() < 0.08:
            on = add_months(disbursal, rng.randint(6, months_ago - 3)).replace(day=1)
            spec.spread_changes.append((on, kw["spread"] + 0.5, "Spread revised +0.50% on risk re-grading"))
        specs.append(spec)
    return specs


def random_card(cust: dict, ids: Ids, rng: random.Random) -> CardSpec:
    product, limit = rng.choice([("Everyday Cashback", [50_000, 100_000, 150_000]),
                                 ("Platinum Rewards", [200_000, 300_000, 500_000])])
    autopay = rng.choices(["FULL", "MIN", None], weights=[60, 10, 30])[0]
    return CardSpec(
        card_id=ids.next("CC", 7).replace("-", ""), customer_id=cust["customer_id"], product=product,
        network=rng.choice(["VISA", "MASTERCARD", "RUPAY"]), last4=f"{rng.randint(0, 9999):04d}",
        credit_limit=rng.choice(limit), autopay_mode=autopay,
        behaviour="REVOLVER" if cust["risk_profile"] == "STRESSED" or rng.random() < 0.15 else "TRANSACTOR",
        spend_level=rng.uniform(0.3, 1.2), bank=rng.choice(BANKS), account_last4=f"{rng.randint(0, 9999):04d}",
    )


def interaction(ids: Ids, cust_id: str, on: str, channel: str, direction: str, summary: str, disposition: str,
                ticket_id: str | None = None) -> dict:
    return {"interaction_id": ids.next("INT"), "customer_id": cust_id, "at": on, "channel": channel,
            "direction": direction, "summary": summary, "disposition": disposition, "ticket_id": ticket_id}


def ticket(t: dict) -> dict:
    sla_days, team = TICKET_CATEGORIES[t["category"]]
    created = date.fromisoformat(t["created_at"][:10])
    sla_due = add_working_days(created, sla_days)
    resolved = t.get("resolved_at")
    breached = (date.fromisoformat(resolved[:10]) if resolved else AS_OF) > sla_due
    return {**t, "assigned_team": t.get("assigned_team") or team, "sla_working_days": sla_days,
            "sla_due_on": sla_due.isoformat(), "sla_breached": breached, "escalation_level": t.get("escalation_level", 0)}


def random_history(cust: dict, loans: list[dict], payments: list[dict], charges: list[dict], ids: Ids,
                   rng: random.Random) -> tuple[list[dict], list[dict]]:
    """CRM interactions and historical tickets derived from what actually happened on the account."""
    interactions, tickets = [], []
    cid = cust["customer_id"]
    for p in payments:
        if p["status"] == "FAILED" and p["presentation_no"] == 1:
            interactions.append(interaction(ids, cid, p["value_date"], "SMS", "OUTBOUND",
                                            f"EMI auto-debit failed: {p['failure_reason'].lower()}.", "NOTIFIED"))
    for c in charges:
        if c["type"] == "BOUNCE_CHARGE" and c["status"] == "PAID" and rng.random() < 0.3:
            on = date.fromisoformat(c["paid_on"]) + timedelta(days=rng.randint(1, 10))
            if on > AS_OF:
                continue
            tid = ids.next("SR")
            approved = rng.random() < 0.5
            tickets.append(ticket({
                "ticket_id": tid.replace("-", ""), "customer_id": cid, "category": "BOUNCE_CHARGE_WAIVER",
                "related_entity": {"type": "CHARGE", "id": c["charge_id"]}, "status": "CLOSED", "priority": "LOW",
                "channel": "CALL", "created_at": f"{on.isoformat()}T12:00:00+05:30",
                "description": f"Customer requested waiver of bounce charge {c['charge_id']}.", "assigned_team": None,
                "resolution": "Waiver approved as goodwill (LP-CHG-004)." if approved else "Waiver declined: not eligible under LP-CHG-004.",
                "resolved_at": f"{add_working_days(on, 2).isoformat()}T16:00:00+05:30",
                "history": [{"at": f"{on.isoformat()}T12:00:00+05:30", "event": "CREATED", "by": "agent", "note": None}],
            }))
            interactions.append(interaction(ids, cid, on.isoformat(), "CALL", "INBOUND",
                                            "Requested bounce charge waiver.", "TICKET_CREATED", tid.replace("-", "")))
    for loan in loans:
        if rng.random() < 0.25:
            on = add_months(date.fromisoformat(loan["disbursal_date"]), rng.randint(1, 12))
            if on <= AS_OF:
                tid = ids.next("SR").replace("-", "")
                tickets.append(ticket({
                    "ticket_id": tid, "customer_id": cid, "category": "STATEMENT_REQUEST",
                    "related_entity": {"type": "LOAN", "id": loan["loan_id"]}, "status": "CLOSED", "priority": "LOW",
                    "channel": "APP", "created_at": f"{on.isoformat()}T10:00:00+05:30",
                    "description": "Repayment statement requested.", "assigned_team": None,
                    "resolution": "Statement emailed to registered address.", "resolved_at": f"{on.isoformat()}T10:00:30+05:30",
                    "history": [{"at": f"{on.isoformat()}T10:00:00+05:30", "event": "CREATED", "by": "app", "note": None}],
                }))
                interactions.append(interaction(ids, cid, on.isoformat(), "APP", "INBOUND",
                                                "Downloaded repayment statement.", "TICKET_CREATED", tid))
    return interactions, tickets


def generate(n_customers: int, out: Path) -> dict:
    ids = Ids()
    ids._n.update({"SR": 100})  # hero tickets are SR0000001..3
    customers, loans, payments, charges, cards, statements, txns, interactions, tickets = ([] for _ in range(9))
    loan_ids = Ids()
    loan_ids._n.update({"PL": 2_000_000, "HL": 2_000_000, "LP": 2_000_000, "CC": 2_000_000})

    # Heroes first, with stable per-entity RNG streams so they never shift when other code changes.
    for cust in personas.CUSTOMERS:
        customers.append(dict(cust))
    for spec in personas.LOANS:
        loan, pays, chgs = build_loan(spec, ids, random.Random(f"{SEED}-{spec.loan_id}"))
        loans.append(loan); payments += pays; charges += chgs
    for spec in personas.CARDS:
        card, sts, tx = build_card(spec, ids, random.Random(f"{SEED}-{spec.card_id}"))
        cards.append(card); statements += sts; txns += tx
    tickets += [ticket(t) for t in personas.TICKETS]

    priya = next(l for l in loans if l["loan_id"] == "HL1000001")
    oct_row = next(r for r in priya["schedule"] if r["due_date"] == "2026-10-05")
    sep_row = next(r for r in priya["schedule"] if r["due_date"] == "2026-09-05")
    rate_notice = (f"Rate revision notice for loan HL1000001: EBLR revised from 8.50% to 9.00% effective 15-Sep-2026. "
                   f"Your rate changes from {sep_row['rate_pct']}% to {oct_row['rate_pct']}% and EMI from "
                   f"Rs {sep_row['emi']:,.0f} to Rs {oct_row['emi']:,.0f} from the EMI due on 05-Oct-2026.")
    for cid, on, ch, direction, summary, disp, tid in personas.INTERACTIONS:
        interactions.append(interaction(ids, cid, on, ch, direction, summary.format(rate_notice=rate_notice), disp, tid))

    dup = next(p for p in payments if p.get("duplicate_of") and p["loan_id"] == "LP1000003")
    sr1 = next(t for t in tickets if t["ticket_id"] == "SR0000001")
    sr1["related_entity"]["payment_id"] = dup["payment_id"]
    dup["dispute_ticket_id"] = "SR0000001"

    # Background population.
    for n in range(1, n_customers + 1):
        rng = random.Random(f"{SEED}-cust-{n}")
        cust = random_customer(n, rng)
        specs = random_loans(cust, loan_ids, rng)
        cust_loans, cust_pays, cust_chgs = [], [], []
        for spec in specs:
            loan, pays, chgs = build_loan(spec, ids, rng)
            cust_loans.append(loan); cust_pays += pays; cust_chgs += chgs
        cust["customer_since"] = min(l["disbursal_date"] for l in cust_loans)
        if rng.random() < 0.4:
            card, sts, tx = build_card(random_card(cust, loan_ids, rng), ids, rng)
            cards.append(card); statements += sts; txns += tx
        ints, tks = random_history(cust, cust_loans, cust_pays, cust_chgs, ids, rng)
        customers.append(cust); loans += cust_loans; payments += cust_pays; charges += cust_chgs
        interactions += ints; tickets += tks

    for c in customers:
        c["kyc_status"] = "VERIFIED"
        c["consents"] = {"marketing": c["customer_id"] != "CUST100002", "whatsapp": True, "voice_recording": True}
    interactions.sort(key=lambda i: (i["customer_id"], i["at"]))

    out.mkdir(parents=True, exist_ok=True)
    files = {
        "crm.json": {"customers": customers, "interactions": interactions},
        "loans.json": {"loans": loans, "charges": charges},
        "payments.json": {"payments": payments},
        "service_ops.json": {"categories": {k: {"sla_working_days": v[0], "team": v[1]} for k, v in TICKET_CATEGORIES.items()},
                             "tickets": tickets},
        "cards.json": {"cards": cards, "statements": statements, "transactions": txns},
    }
    for name, payload in files.items():
        (out / name).write_text(json.dumps(payload, indent=1, default=str))
    counts = {"customers": len(customers), "loans": len(loans), "active_loans": sum(l["status"] == "ACTIVE" for l in loans),
              "payments": len(payments), "failed_payments": sum(p["status"] == "FAILED" for p in payments),
              "charges": len(charges), "cards": len(cards), "statements": len(statements),
              "card_transactions": len(txns), "interactions": len(interactions), "tickets": len(tickets)}
    manifest = {"seed": SEED, "as_of": AS_OF.isoformat(), "counts": counts, "files": sorted(files)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", type=int, default=200, help="background customers in addition to the demo personas")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "generated")
    args = ap.parse_args()
    manifest = generate(args.customers, args.out)
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
