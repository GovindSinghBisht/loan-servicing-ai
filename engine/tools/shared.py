"""Helpers shared by tool packs: ownership checks, policy lookup and matching, dates."""
from __future__ import annotations

from datetime import date

from ..clients import NotFound
from ..core import Session, ToolError


async def owned_loan(s: Session, loan_id: str) -> dict:
    try:
        loan = await s.systems.loans.get(f"/loans/{loan_id}")
    except NotFound:
        loan = None
    if not loan or loan["customer_id"] != s.customer_id:
        raise ToolError("NOT_FOUND", f"No loan {loan_id} on this customer's profile.")
    return loan


async def customer_loans(s: Session) -> list[dict]:
    return (await s.systems.loans.get(f"/customers/{s.customer_id}/loans"))["loans"]


async def clauses(s: Session, product: str, category: str) -> list[dict]:
    return (await s.systems.policy.get("/clauses", product=product, category=category))["clauses"]


async def clause(s: Session, clause_id: str) -> dict:
    return await s.systems.policy.get(f"/clauses/{clause_id}")


def match(candidates: list[dict], loan: dict) -> dict | None:
    """The clause whose product / rate-type / borrower-type scope fits this loan (most specific first)."""
    fits = [c for c in candidates
            if loan["product"] in c["products"]
            and loan["rate_type"] in c.get("rate_types", [loan["rate_type"]])
            and loan["borrower_type"] in c.get("borrower_types", [loan["borrower_type"]])]
    fits.sort(key=lambda c: -(("rate_types" in c) + ("borrower_types" in c)))
    return fits[0] if fits else None


def by_id(candidates: list[dict], clause_id: str) -> dict:
    return next(c for c in candidates if c["id"] == clause_id)


def cite(c: dict) -> str:
    return f"policy:{c['id']} v{c['version']}"


def fy_start(d: date) -> date:
    """Indian financial year starts 1 April."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def check(rule: str, passed: bool, detail: str) -> dict:
    return {"rule": rule, "passed": passed, "detail": detail}


def r2(x: float) -> float:
    return round(x, 2)
