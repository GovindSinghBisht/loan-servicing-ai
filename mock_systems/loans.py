"""Loan Management System: loan accounts, repayment schedules, rate history, charges ledger. Read-only."""
from __future__ import annotations

from .common import load, make_app, not_found

app = make_app("loans")
_db = load("loans")
loans = {l["loan_id"]: l for l in _db["loans"]}
charges = _db["charges"]


def _get(loan_id: str) -> dict:
    if loan_id not in loans:
        raise not_found("loan", loan_id)
    return loans[loan_id]


def _summary(loan: dict) -> dict:
    return {k: v for k, v in loan.items() if k not in ("schedule", "rate_history")}


@app.get("/customers/{customer_id}/loans")
def customer_loans(customer_id: str, status: str | None = None):
    rows = [_summary(l) for l in loans.values() if l["customer_id"] == customer_id
            and (status is None or l["status"] == status)]
    return {"loans": rows}


@app.get("/loans/{loan_id}")
def get_loan(loan_id: str):
    return _summary(_get(loan_id))


@app.get("/loans/{loan_id}/schedule")
def get_schedule(loan_id: str, from_date: str | None = None, to_date: str | None = None):
    rows = [r for r in _get(loan_id)["schedule"]
            if (from_date is None or r["due_date"] >= from_date) and (to_date is None or r["due_date"] <= to_date)]
    return {"loan_id": loan_id, "instalments": rows}


@app.get("/loans/{loan_id}/rate-history")
def get_rate_history(loan_id: str):
    return {"loan_id": loan_id, "rate_history": _get(loan_id)["rate_history"]}


@app.get("/loans/{loan_id}/charges")
def get_charges(loan_id: str, status: str | None = None):
    _get(loan_id)
    return {"loan_id": loan_id, "charges": [c for c in charges if c["loan_id"] == loan_id
                                            and (status is None or c["status"] == status)]}
