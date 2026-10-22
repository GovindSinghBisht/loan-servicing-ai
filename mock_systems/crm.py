"""Customer CRM: identity, contact details, consents, interaction history."""
from __future__ import annotations

from fastapi import Request
from pydantic import BaseModel

from .common import idempotent, load, make_app, not_found, now_iso

app = make_app("crm")
_db = load("crm")
customers = {c["customer_id"]: c for c in _db["customers"]}
by_phone = {c["phone"].replace("-", ""): c for c in _db["customers"]}
interactions = _db["interactions"]


@app.get("/customers/lookup")
def lookup(phone: str):
    c = by_phone.get(phone.replace("-", "").replace(" ", ""))
    if not c:
        raise not_found("customer", phone)
    return c


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    if customer_id not in customers:
        raise not_found("customer", customer_id)
    return customers[customer_id]


@app.get("/customers/{customer_id}/interactions")
def list_interactions(customer_id: str, limit: int = 20):
    rows = [i for i in interactions if i["customer_id"] == customer_id]
    return {"interactions": rows[-limit:]}


class NewInteraction(BaseModel):
    channel: str
    direction: str = "INBOUND"
    summary: str
    disposition: str
    ticket_id: str | None = None


@app.post("/customers/{customer_id}/interactions", status_code=201)
def log_interaction(customer_id: str, body: NewInteraction, request: Request):
    if customer_id not in customers:
        raise not_found("customer", customer_id)

    def build():
        row = {"interaction_id": f"INT-{len(interactions) + 1:07d}", "customer_id": customer_id,
               "at": now_iso(), **body.model_dump()}
        interactions.append(row)
        return row
    return idempotent(app, request, build)
