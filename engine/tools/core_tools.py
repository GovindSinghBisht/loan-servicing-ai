"""Tools every workflow pack gets: customer overview, service requests, escalation, confirm/cancel."""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from ..clients import SystemUnavailable
from ..core import Proposal, Session, ToolError, ToolResult, tool


async def _soft(coro, default):
    """Overview is best-effort: a slow or failed system degrades to a note instead of failing the call."""
    try:
        return await coro, None
    except SystemUnavailable as e:
        return default, e.system


@tool("Snapshot of the customer: profile, all loans and cards with key figures, and open service requests. "
      "Use it to resolve which loan/card the customer means.")
async def get_customer_overview(s: Session, _) -> ToolResult:
    sy = s.systems
    (profile, e1), (loans, e2), (cards, e3), (tickets, e4) = await asyncio.gather(
        _soft(sy.crm.get(f"/customers/{s.customer_id}"), None),
        _soft(sy.loans.get(f"/customers/{s.customer_id}/loans"), {"loans": []}),
        _soft(sy.cards.get(f"/customers/{s.customer_id}/cards"), {"cards": []}),
        _soft(sy.service_ops.get(f"/customers/{s.customer_id}/tickets"), {"tickets": []}),
    )
    unavailable = [x for x in (e1, e2, e3, e4) if x]
    loan_keys = ("loan_id", "product", "status", "rate_type", "current_rate_pct", "current_emi", "next_due_date",
                 "principal_outstanding", "emis_paid", "emis_remaining", "overdue_amount", "unpaid_charges",
                 "reset_preference", "sanctioned_amount", "disbursal_date")
    card_keys = ("card_id", "product", "card_number_masked", "status", "credit_limit", "outstanding",
                 "available_limit", "autopay")
    data = {
        "business_date": await sy.business_date(),
        "customer": {k: profile[k] for k in ("customer_id", "first_name", "last_name", "preferred_language", "segment",
                                              "customer_since")} if profile else None,
        "loans": [{k: l[k] for k in loan_keys} for l in loans["loans"] if l["status"] == "ACTIVE"],
        "closed_loans": [l["loan_id"] for l in loans["loans"] if l["status"] == "CLOSED"],
        "cards": [{k: c[k] for k in card_keys} for c in cards["cards"]],
        "open_service_requests": [
            {k: t[k] for k in ("ticket_id", "category", "status", "created_at", "sla_due_on", "sla_breached", "related_entity")}
            for t in tickets["tickets"] if t["status"] not in ("RESOLVED", "CLOSED")],
        "unavailable_systems": unavailable,
    }
    sources = [f"crm:{s.customer_id}"] + [f"loans:{l['loan_id']}" for l in data["loans"]] + \
              [f"cards:{c['card_id']}" for c in data["cards"]]
    return ToolResult(data, sources)


class TicketFilter(BaseModel):
    status: str | None = Field(None, description="OPEN, IN_PROGRESS, ESCALATED, RESOLVED or CLOSED; omit for all")


@tool("List the customer's service requests (tickets) with status, SLA due date and whether the SLA is breached.",
      TicketFilter)
async def list_service_requests(s: Session, a: TicketFilter) -> ToolResult:
    rows = (await s.systems.service_ops.get(f"/customers/{s.customer_id}/tickets", status=a.status))["tickets"]
    return ToolResult({"tickets": rows[:15]}, [f"service_ops:{t['ticket_id']}" for t in rows[:15]])


class EscalateArgs(BaseModel):
    ticket_id: str
    reason: str = Field(description="Why it needs escalating, in the customer's words")


@tool("Escalate an open service request, e.g. when its SLA is breached. Requires customer confirmation.",
      EscalateArgs, kind="action")
async def escalate_service_request(s: Session, a: EscalateArgs) -> Proposal:
    t = await s.systems.service_ops.get(f"/tickets/{a.ticket_id}")
    if t["customer_id"] != s.customer_id:
        raise ToolError("NOT_FOUND", f"No service request {a.ticket_id} on this customer's profile.")
    if t["status"] in ("RESOLVED", "CLOSED"):
        raise ToolError("TICKET_CLOSED", f"{a.ticket_id} is already {t['status'].lower()}.")

    async def run() -> ToolResult:
        out = await s.systems.service_ops.post(f"/tickets/{a.ticket_id}/escalate", {"reason": a.reason},
                                               idempotency_key=f"{s.session_id}:esc:{a.ticket_id}")
        return ToolResult({k: out[k] for k in ("ticket_id", "status", "escalation_level", "priority", "sla_due_on")},
                          [f"service_ops:{a.ticket_id}"])

    return Proposal(
        summary=f"Escalate {a.ticket_id} ({t['category'].replace('_', ' ').lower()}, SLA due {t['sla_due_on']}"
                f"{', breached' if t['sla_breached'] else ''}) to priority handling.",
        details={"ticket_id": a.ticket_id, "category": t["category"], "sla_due_on": t["sla_due_on"],
                 "sla_breached": t["sla_breached"], "reason": a.reason},
        run=run,
    )


class ActionRef(BaseModel):
    action_id: str


@tool("Execute a previously proposed action after the customer has explicitly confirmed it in their latest message.",
      ActionRef)
async def confirm_action(s: Session, a: ActionRef) -> ToolResult:
    pending = s.pending.get(a.action_id)
    if not pending:
        raise ToolError("NO_SUCH_ACTION", "That action is not pending (already done, cancelled or never proposed).")
    if pending.proposed_in_turn >= s.turn:
        raise ToolError("CONFIRMATION_REQUIRED", "The customer has not replied since this was proposed. Ask them to "
                                                 "confirm first; only their next message can confirm it.")
    out = await pending.proposal.run()
    del s.pending[a.action_id]
    s.executed_actions += 1
    s.log("action_executed", action_id=a.action_id, tool=pending.tool, proposed_in_turn=pending.proposed_in_turn,
          details=pending.proposal.details, result=out.data)
    return out


@tool("Cancel a proposed action the customer declined or changed their mind about.", ActionRef)
async def cancel_action(s: Session, a: ActionRef) -> ToolResult:
    pending = s.pending.pop(a.action_id, None)
    if pending:
        s.log("action_cancelled", action_id=a.action_id, tool=pending.tool)
    return ToolResult({"cancelled": bool(pending), "action_id": a.action_id})
