"""Payments system: every debit attempt, failure reasons, duplicate debits and refunds. Read-only."""
from __future__ import annotations

from .common import load, make_app, not_found

app = make_app("payments")
payments = load("payments")["payments"]
by_id = {p["payment_id"]: p for p in payments}


@app.get("/loans/{loan_id}/payments")
def loan_payments(loan_id: str, since: str | None = None, until: str | None = None, status: str | None = None,
                  purpose: str | None = None):
    rows = [p for p in payments if p["loan_id"] == loan_id
            and (since is None or p["value_date"] >= since) and (until is None or p["value_date"] <= until)
            and (status is None or p["status"] == status) and (purpose is None or p["purpose"] == purpose)]
    return {"loan_id": loan_id, "payments": rows}


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str):
    if payment_id not in by_id:
        raise not_found("payment", payment_id)
    return by_id[payment_id]
