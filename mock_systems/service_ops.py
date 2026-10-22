"""Service Operations: service requests (tickets) with SLA tracking, creation and escalation."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .common import business_date, idempotent, load, make_app, not_found, now_iso

app = make_app("service_ops")
_db = load("service_ops")
categories = _db["categories"]
tickets = {t["ticket_id"]: t for t in _db["tickets"]}


def add_working_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _refresh_sla(t: dict) -> dict:
    """SLA breach is evaluated against the business date at read time, not frozen at generation."""
    if t["status"] not in ("RESOLVED", "CLOSED"):
        t["sla_breached"] = business_date().isoformat() > t["sla_due_on"]
    return t


@app.get("/categories")
def list_categories():
    return categories


@app.get("/customers/{customer_id}/tickets")
def customer_tickets(customer_id: str, status: str | None = None):
    rows = [_refresh_sla(t) for t in tickets.values() if t["customer_id"] == customer_id
            and (status is None or t["status"] == status)]
    return {"tickets": sorted(rows, key=lambda t: t["created_at"], reverse=True)}


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    if ticket_id not in tickets:
        raise not_found("ticket", ticket_id)
    return _refresh_sla(tickets[ticket_id])


class NewTicket(BaseModel):
    customer_id: str
    category: str
    related_entity: dict
    description: str
    channel: str = "AI_ASSISTANT"
    priority: str = "MEDIUM"
    details: dict = {}


@app.post("/tickets", status_code=201)
def create_ticket(body: NewTicket, request: Request):
    if body.category not in categories:
        raise HTTPException(422, {"error": "UNKNOWN_CATEGORY", "allowed": sorted(categories)})

    def build():
        sla = categories[body.category]
        created = now_iso()
        t = {
            "ticket_id": f"SR{len(tickets) + 5000:07d}", **body.model_dump(), "status": "OPEN",
            "created_at": created, "assigned_team": sla["team"], "resolution": None, "resolved_at": None,
            "sla_working_days": sla["sla_working_days"],
            "sla_due_on": add_working_days(business_date(), sla["sla_working_days"]).isoformat(),
            "sla_breached": False, "escalation_level": 0,
            "history": [{"at": created, "event": "CREATED", "by": body.channel.lower(), "note": None}],
        }
        tickets[t["ticket_id"]] = t
        return t
    return idempotent(app, request, build)


class Escalation(BaseModel):
    reason: str
    by: str = "ai_assistant"


@app.post("/tickets/{ticket_id}/escalate")
def escalate(ticket_id: str, body: Escalation, request: Request):
    if ticket_id not in tickets:
        raise not_found("ticket", ticket_id)
    t = tickets[ticket_id]
    if t["status"] in ("RESOLVED", "CLOSED"):
        raise HTTPException(409, {"error": "TICKET_CLOSED", "status": t["status"]})

    def build():
        t["escalation_level"] += 1
        t["status"] = "ESCALATED"
        t["priority"] = "HIGH"
        t["history"].append({"at": now_iso(), "event": "ESCALATED", "by": body.by, "note": body.reason})
        return _refresh_sla(t)
    return idempotent(app, request, build)
