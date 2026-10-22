"""Policy Knowledge: versioned product clauses with machine-readable parameters."""
from __future__ import annotations

import json

from .common import POLICY_DIR, business_date, make_app, not_found

app = make_app("policy")
domains = {p.stem: json.loads(p.read_text()) for p in POLICY_DIR.glob("*.json")}
clauses = [{**c, "domain": d["domain"]} for d in domains.values() for c in d["clauses"]]


def _in_force(c: dict, on: str) -> bool:
    return c["effective_from"] <= on and (c["effective_to"] is None or on <= c["effective_to"])


@app.get("/clauses")
def search(product: str | None = None, category: str | None = None, as_of: str | None = None,
           include_superseded: bool = False):
    """Clauses in force on `as_of` (default: business date). Superseded versions only when asked for."""
    on = as_of or business_date().isoformat()
    rows = [c for c in clauses
            if (product is None or product in c["products"]) and (category is None or c["category"] == category)
            and (include_superseded or _in_force(c, on))]
    return {"as_of": on, "clauses": rows}


@app.get("/clauses/{clause_id}")
def get_clause(clause_id: str, as_of: str | None = None):
    on = as_of or business_date().isoformat()
    versions = [c for c in clauses if c["id"] == clause_id]
    if not versions:
        raise not_found("clause", clause_id)
    current = next((c for c in versions if _in_force(c, on)), versions[-1])
    return {**current, "all_versions": [{"version": v["version"], "status": v["status"],
                                         "effective_from": v["effective_from"], "effective_to": v["effective_to"]}
                                        for v in versions]}


@app.get("/globals")
def get_globals():
    return {d["domain"]: d["globals"] for d in domains.values()}
