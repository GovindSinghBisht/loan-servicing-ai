"""A keyword-routed stand-in for the LLM, for UI development and API tests without an API key.

It calls the real tools (so data, rules, security and confirmations are exercised end to end) and
writes plain replies from their results. It is deliberately simple and is not used in production.

    uv run python -m tests.offline_server     # API on :8000 backed by this provider
"""
from __future__ import annotations

import json
import re

from engine.llm import Prompt, Step, ToolCall


def _rs(x) -> str:
    return f"Rs {x:,.0f}" if isinstance(x, (int, float)) else str(x)


class OfflineProvider:
    name, model = "offline", "keyword-router"

    def __init__(self):
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"call_{self._n}"

    def user_message(self, text: str) -> list:
        return [{"role": "user", "content": text}]

    def tool_results(self, calls: list[ToolCall], outputs: list[dict]) -> list:
        return [{"role": "tool", "name": c.name, "content": o} for c, o in zip(calls, outputs)]

    async def step(self, prompt: Prompt, history: list, on_text=None) -> Step:
        last = history[-1]
        if last["role"] == "user":
            calls = self._route(last["content"], json.loads(prompt.dynamic.split("\n", 1)[1]), history)
            if calls:
                return Step("", calls, "tool_use", assistant_message=[{"role": "assistant", "calls": calls}])
            text = ("I can explain EMI changes, failed payments, prepayment and foreclosure, your options, and "
                    "raise or escalate requests. What would you like to check?")
        else:
            results = [m for m in reversed(history) if m["role"] == "tool"]
            batch = []
            for m in results:
                batch.insert(0, m)
                if history.index(m) > 0 and history[history.index(m) - 1]["role"] != "tool":
                    break
            text = " ".join(self._describe(m["name"], m["content"]) for m in batch)
        for word in re.findall(r"\S+\s*", text):
            if on_text:
                on_text(word)
        return Step(text, [], "end", assistant_message=[{"role": "assistant", "content": text}])

    def _route(self, text: str, snapshot: dict, history: list) -> list[ToolCall]:
        t = text.lower()
        loans = snapshot.get("loans") or []
        loan = next((l for l in loans if l["product"].split("_")[0].lower() in t), loans[0] if loans else None)
        lid = {"loan_id": loan["loan_id"]} if loan else {}
        pending = [m["content"]["action_id"] for m in history
                   if m["role"] == "tool" and isinstance(m["content"], dict) and m["content"].get("action_id")]
        amount = re.search(r"(\d[\d,]{3,})", t)
        if re.search(r"\b(yes|confirm|go ahead|sure)\b", t) and pending:
            return [ToolCall(self._id(), "confirm_action", {"action_id": pending[-1]})]
        if "escalat" in t:
            tickets = snapshot.get("open_service_requests") or []
            if tickets:
                return [ToolCall(self._id(), "escalate_service_request",
                                 {"ticket_id": tickets[0]["ticket_id"], "reason": text})]
        if "prepay" in t and amount and lid:
            return [ToolCall(self._id(), "prepayment_quote", {**lid, "amount": float(amount.group(1).replace(",", ""))})]
        if "foreclos" in t and lid:
            return [ToolCall(self._id(), "foreclosure_quote", lid)]
        if "option" in t and lid:
            return [ToolCall(self._id(), "get_loan_options", lid)]
        calls = []
        if "emi" in t and lid and re.search(r"why|increase|differ|change|went up", t):
            calls.append(ToolCall(self._id(), "explain_emi_change", lid))
        if re.search(r"fail|bounce|debit|payment", t) and lid:
            calls.append(ToolCall(self._id(), "get_payment_history", lid))
        if not calls and re.search(r"request|ticket|complaint|status", t):
            calls.append(ToolCall(self._id(), "list_service_requests", {}))
        if not calls and "emi" in t and lid:
            calls.append(ToolCall(self._id(), "get_loan_details", lid))
        return calls

    def _describe(self, name: str, out: dict) -> str:
        if not out.get("ok"):
            return out.get("message", "That isn't available right now.")
        if out.get("status") == "CONFIRMATION_REQUIRED":
            return f"{out['summary']} Shall I go ahead?"
        d = out["data"]
        if name == "explain_emi_change":
            r = d["reasons"][0]
            why = (f"the rate changed from {r['from_rate']}% to {r['to_rate']}% ({r.get('reason', '')})"
                   if r["type"] == "RATE_RESET" else r["type"].replace("_", " ").lower())
            return (f"Your EMI is {_rs(d['explained_emi']['emi'])}, previously {_rs(d['previous_emi']['emi'])}, "
                    f"because {why}.")
        if name == "get_payment_history":
            inc = d["failed_debit_incidents"]
            if not inc:
                return "No EMI debits failed in that period."
            return "Failed debits: " + "; ".join(
                f"{i['due_date']} ({i['attempts'][0]['failure_reason']}), {i['outcome']}" for i in inc) + "."
        if name == "prepayment_quote":
            if not d["eligible"]:
                return "Prepayment isn't allowed yet: " + "; ".join(c["detail"] for c in d["checks"] if not c["passed"]) + "."
            i = d["impact"].get("REDUCE_TENURE") or next(iter(d["impact"].values()))
            return (f"Prepaying {_rs(d['amount'])} costs {_rs(d['charges']['total'])} in charges and saves about "
                    f"{_rs(i['interest_saved'])} in interest ({i['months_saved']} months sooner).")
        if name == "foreclosure_quote":
            if not d["eligible"]:
                return "Foreclosure isn't allowed yet: " + "; ".join(c["detail"] for c in d["checks"] if not c["passed"]) + "."
            return f"To close the loan today you'd pay {_rs(d['amount_payable'])}, valid until {d['valid_until']}."
        if name == "get_loan_options":
            return "Your options: " + ", ".join(
                f"{o['option'].replace('_', ' ').lower()} ({'available' if o['available'] else 'not available'})"
                for o in d["options"]) + "."
        if name == "list_service_requests":
            return "Your requests: " + "; ".join(f"{t['ticket_id']} {t['category'].replace('_', ' ').lower()} "
                                                 f"({t['status'].lower()})" for t in d["tickets"][:4]) + "."
        if name == "confirm_action":
            return f"Done. Your reference is {d.get('ticket_id')}" + (f", due by {d['sla_due_on']}." if d.get("sla_due_on") else ".")
        if name == "get_loan_details":
            return f"Your EMI on {d['loan_id']} is {_rs(d['current_emi'])}, next due {d['next_due_date']}."
        return "Done."
