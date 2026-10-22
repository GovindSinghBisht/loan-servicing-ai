"""Deterministic loan math, shared by the data generator and the query engine's tools.

The LLM never does arithmetic: every number in an answer comes from here.

Conventions (deliberate simplifications, documented so answers stay explainable):
- Monthly-rest reducing balance: interest for a cycle = opening principal * annual rate / 12.
- EMIs are rounded to the nearest rupee; the final instalment absorbs rounding.
- A rate change applies from the first instalment due *after* its effective date.
- Broken-period interest (disbursal -> start of the first EMI cycle) is added to EMI #1.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

ResetMode = Literal["EMI", "TENURE"]
PrepayMode = Literal["REDUCE_EMI", "REDUCE_TENURE"]


def add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def emi(principal: float, annual_rate_pct: float, months: int) -> float:
    r = annual_rate_pct / 1200
    if r == 0:
        return principal / months
    f = (1 + r) ** months
    return principal * r * f / (f - 1)


def remaining_tenure(principal: float, annual_rate_pct: float, emi_amount: float) -> int:
    """Months needed to repay `principal` at a fixed EMI. Raises if the EMI doesn't cover interest."""
    r = annual_rate_pct / 1200
    if emi_amount <= principal * r:
        raise ValueError("EMI does not cover monthly interest")
    n = -math.log(1 - principal * r / emi_amount) / math.log(1 + r)
    return math.ceil(n - 1e-9)


@dataclass
class RateChange:
    effective: date
    annual_rate_pct: float
    reason: str


@dataclass
class PartPrepayment:
    on: date
    amount: float
    mode: PrepayMode


@dataclass
class Instalment:
    number: int
    due_date: date
    rate_pct: float
    opening_principal: float
    emi: float
    interest: float
    principal: float
    closing_principal: float
    broken_period_interest: float = 0.0
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["due_date"] = self.due_date.isoformat()
        return d


def build_schedule(
    principal: float,
    tenure_months: int,
    disbursal_date: date,
    first_due_date: date,
    rate_changes: list[RateChange],
    reset_mode: ResetMode = "EMI",
    prepayments: list[PartPrepayment] | None = None,
    max_tenure_months: int | None = None,
) -> list[Instalment]:
    """Full amortization schedule, replaying rate resets and part-prepayments in date order.

    In TENURE mode, if a reset would push total tenure past `max_tenure_months`, the EMI is revised instead.
    """
    changes = sorted(rate_changes, key=lambda c: c.effective)
    starting = [c for c in changes if c.effective <= disbursal_date]
    if not starting:
        raise ValueError("need a rate effective on or before disbursal")
    rate = starting[-1].annual_rate_pct
    pending_rates = [c for c in changes if c.effective > disbursal_date]
    pending_prepay = sorted(prepayments or [], key=lambda p: p.on)

    outstanding = float(principal)
    remaining = tenure_months
    cur_emi = round(emi(principal, rate, tenure_months))
    schedule: list[Instalment] = []
    due = first_due_date
    n = 1

    while outstanding > 0.005:
        if n > 1000:
            raise RuntimeError("schedule did not converge")
        events: list[str] = []

        while pending_rates and pending_rates[0].effective < due:
            rc = pending_rates.pop(0)
            if rc.annual_rate_pct == rate:
                continue
            old_rate, rate = rate, rc.annual_rate_pct
            if reset_mode == "EMI":
                old_emi, cur_emi = cur_emi, round(emi(outstanding, rate, remaining))
                events.append(f"RATE_RESET {old_rate}%->{rate}% ({rc.reason}); EMI {old_emi}->{cur_emi}")
            else:
                try:
                    new_rem = remaining_tenure(outstanding, rate, cur_emi)
                except ValueError:
                    new_rem = None
                if new_rem is not None and (max_tenure_months is None or n - 1 + new_rem <= max_tenure_months):
                    events.append(f"RATE_RESET {old_rate}%->{rate}% ({rc.reason}); tenure {remaining}->{new_rem} months")
                    remaining = new_rem
                else:
                    old_emi, cur_emi = cur_emi, round(emi(outstanding, rate, remaining))
                    events.append(f"RATE_RESET {old_rate}%->{rate}% ({rc.reason}); EMI {old_emi}->{cur_emi} (max tenure reached)")

        while pending_prepay and pending_prepay[0].on < due:
            pp = pending_prepay.pop(0)
            outstanding = round(outstanding - pp.amount, 2)
            if pp.mode == "REDUCE_EMI":
                old_emi, cur_emi = cur_emi, round(emi(outstanding, rate, remaining))
                events.append(f"PART_PREPAYMENT {pp.amount:.0f}; EMI {old_emi}->{cur_emi}")
            else:
                old_rem, remaining = remaining, remaining_tenure(outstanding, rate, cur_emi)
                events.append(f"PART_PREPAYMENT {pp.amount:.0f}; tenure {old_rem}->{remaining} months")

        interest = round(outstanding * rate / 1200, 2)
        principal_part = round(cur_emi - interest, 2)
        if remaining <= 1 or principal_part >= outstanding:
            principal_part = round(outstanding, 2)
        payment = round(principal_part + interest, 2)

        bpi = 0.0
        if n == 1:
            cycle_start = add_months(first_due_date, -1)
            days = (cycle_start - disbursal_date).days
            if days > 0:
                bpi = round(principal * rate / 100 * days / 365, 2)
                events.append(f"BROKEN_PERIOD_INTEREST {days} days")

        closing = round(outstanding - principal_part, 2)
        schedule.append(Instalment(
            number=n, due_date=due, rate_pct=rate, opening_principal=round(outstanding, 2),
            emi=round(payment + bpi, 2), interest=round(interest + bpi, 2), principal=principal_part,
            closing_principal=closing, broken_period_interest=bpi, events=events,
        ))
        outstanding = closing
        remaining -= 1
        due = add_months(due, 1)
        n += 1

    return schedule


@dataclass
class Projection:
    months: int
    total_interest: float
    emi: float
    last_payment: float


def project(outstanding: float, annual_rate_pct: float, emi_amount: float) -> Projection:
    """Repay `outstanding` at a constant rate and EMI; returns tenure and total future interest."""
    bal, total, months, last = outstanding, 0.0, 0, 0.0
    while bal > 0.005:
        if months > 1000:
            raise ValueError("EMI too small to repay the loan")
        interest = round(bal * annual_rate_pct / 1200, 2)
        principal = min(round(emi_amount - interest, 2), bal)
        if principal <= 0:
            raise ValueError("EMI does not cover monthly interest")
        if bal - principal <= emi_amount * 0.05:  # final instalment absorbs the EMI rounding residual
            principal = bal
        last = round(principal + interest, 2)
        total += interest
        bal = round(bal - principal, 2)
        months += 1
    return Projection(months, round(total, 2), emi_amount, last)


@dataclass
class PrepaymentImpact:
    amount: float
    mode: PrepayMode
    before: Projection
    after: Projection
    interest_saved: float
    months_saved: int
    emi_change: float


def prepayment_impact(
    outstanding: float, annual_rate_pct: float, emi_amount: float, amount: float, mode: PrepayMode
) -> PrepaymentImpact:
    """Interest saved by prepaying `amount` now, assuming the current rate holds. Excludes charges."""
    if not 0 < amount < outstanding:
        raise ValueError("prepayment must be positive and below outstanding principal")
    before = project(outstanding, annual_rate_pct, emi_amount)
    bal = outstanding - amount
    if mode == "REDUCE_TENURE":
        after = project(bal, annual_rate_pct, emi_amount)
    else:
        after = project(bal, annual_rate_pct, round(emi(bal, annual_rate_pct, before.months)))
    return PrepaymentImpact(
        amount=amount, mode=mode, before=before, after=after,
        interest_saved=round(before.total_interest - after.total_interest, 2),
        months_saved=before.months - after.months,
        emi_change=round(after.emi - before.emi, 2),
    )
