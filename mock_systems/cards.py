"""Cards system (second workflow): cards, statements, transactions, disputes, card block."""
from __future__ import annotations

from fastapi import Request
from pydantic import BaseModel

from .common import idempotent, load, make_app, not_found, now_iso

app = make_app("cards")
_db = load("cards")
cards = {c["card_id"]: c for c in _db["cards"]}
statements = _db["statements"]
transactions = _db["transactions"]


def _get(card_id: str) -> dict:
    if card_id not in cards:
        raise not_found("card", card_id)
    return cards[card_id]


@app.get("/customers/{customer_id}/cards")
def customer_cards(customer_id: str):
    return {"cards": [c for c in cards.values() if c["customer_id"] == customer_id]}


@app.get("/cards/{card_id}")
def get_card(card_id: str):
    return _get(card_id)


@app.get("/cards/{card_id}/statements")
def get_statements(card_id: str, limit: int = 6):
    _get(card_id)
    rows = [s for s in statements if s["card_id"] == card_id]
    return {"card_id": card_id, "statements": rows[-limit:]}


@app.get("/cards/{card_id}/transactions")
def get_transactions(card_id: str, since: str | None = None, until: str | None = None, type: str | None = None,
                     disputed: bool | None = None):
    _get(card_id)
    rows = [t for t in transactions if t["card_id"] == card_id
            and (since is None or t["txn_date"] >= since) and (until is None or t["txn_date"] <= until)
            and (type is None or t["type"] == type) and (disputed is None or bool(t["dispute"]) == disputed)]
    return {"card_id": card_id, "transactions": rows}


class Block(BaseModel):
    reason: str


@app.post("/cards/{card_id}/block")
def block_card(card_id: str, body: Block, request: Request):
    card = _get(card_id)

    def build():
        if card["status"] != "BLOCKED":
            card.update(status="BLOCKED", blocked_at=now_iso(), block_reason=body.reason)
        return card
    return idempotent(app, request, build)
