"""Loan Management System: loan accounts, repayment schedules, rate history, charges ledger. Read-only."""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .common import business_date, load, make_app, not_found
from .statements import Statement, build_statement

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


# ---- documents (the lender's statement service) ---------------------------------------------------------

customers = {c["customer_id"]: c for c in load("crm")["customers"]}  # borrower names for documents
documents: dict[str, Statement] = {}


class StatementRequest(BaseModel):
    financial_year: str  # e.g. "2025-26"


@app.post("/loans/{loan_id}/statements", status_code=201)
def issue_statement(loan_id: str, body: StatementRequest):
    loan = _get(loan_id)
    fy = body.financial_year
    if len(fy) != 7 or fy[4] != "-" or not (fy[:4] + fy[5:]).isdigit() or int(fy[5:]) != (int(fy[:4]) + 1) % 100:
        raise HTTPException(422, {"error": "INVALID_FINANCIAL_YEAR", "expected": "YYYY-YY, e.g. 2025-26"})
    if f"{int(fy[:4]) + 1}-03-31" < loan["disbursal_date"]:
        raise HTTPException(422, {"error": "BEFORE_DISBURSAL", "disbursal_date": loan["disbursal_date"]})
    doc = build_statement(loan, customers.get(loan["customer_id"]),
                          [c for c in charges if c["loan_id"] == loan_id], fy, business_date())
    documents[doc.document_id] = doc
    return doc.public()


@app.get("/documents/{document_id}")
def get_document(document_id: str, customer_id: str):
    """Serve a document only to the customer it belongs to (ownership is checked here, at the source)."""
    doc = documents.get(document_id)
    if not doc or doc.customer_id != customer_id or not doc.path.exists():
        raise not_found("document", document_id)
    return FileResponse(doc.path, media_type="application/pdf", filename=doc.file_name)
