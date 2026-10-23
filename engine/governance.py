"""Guardrails and governance: one place for the rules that decide what reaches the model, what leaves it,
and which actions may run. Rules come from data/governance/policy.json so risk teams can review and
change them without code changes.

Stages:
- input:  block secrets (OTP/PIN/CVV/password), redact card numbers / Aadhaar / PAN before the LLM sees
          them, flag prompt-injection attempts, cap length.
- output: grounding check, every amount, percentage and account id in a reply must appear in data the
          tools returned in this conversation; anything else is flagged for review.
- action: kill switches, channel restrictions, per-session action cap and amount limits, enforced in the
          engine before a proposal is made (the model cannot talk its way past them).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "governance" / "policy.json"


@lru_cache(maxsize=1)
def policy() -> dict:
    return json.loads(POLICY_PATH.read_text())


@dataclass
class GuardrailEvent:
    stage: str      # input | output | action | channel
    rule: str
    severity: str   # info | warn | block
    detail: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------------------------
# PII detection and redaction (also used to mask data sent to observability)
# --------------------------------------------------------------------------------------------------------

_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_SECRET = re.compile(r"\b(otp|one[- ]time password|pin|cvv|password|passcode)\b[^\d]{0,20}\d{3,8}\b", re.I)


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact(text: str, kinds: list[str] | None = None) -> tuple[str, list[str]]:
    """Replace sensitive identifiers with placeholders. Returns the clean text and the kinds found."""
    kinds = kinds if kinds is not None else ["card_number", "aadhaar", "pan"]
    found: list[str] = []

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card_number")
            return "[card number removed]"
        return m.group(0)

    if "card_number" in kinds:
        text = _CARD.sub(card, text)
    if "aadhaar" in kinds and _AADHAAR.search(text):
        found.append("aadhaar")
        text = _AADHAAR.sub("[Aadhaar number removed]", text)
    if "pan" in kinds and _PAN.search(text):
        found.append("pan")
        text = _PAN.sub("[PAN removed]", text)
    return text, found


def mask_for_traces(data: Any) -> Any:
    """Recursively redact strings before they leave the system (e.g. to Langfuse)."""
    if isinstance(data, str):
        return redact(data)[0]
    if isinstance(data, dict):
        return {k: mask_for_traces(v) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_for_traces(v) for v in data]
    return data


# --------------------------------------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------------------------------------

_INJECTION = re.compile(
    r"ignore (all |any |the |your )?(previous |prior |above )?(instructions|rules|prompt)|system prompt|"
    r"you are now|developer mode|jailbreak|pretend (to be|you are)|disregard (your|the) (rules|instructions)", re.I)

SECRET_REPLY = ("For your security, please don't share an OTP, PIN, CVV or password with anyone, including me. "
                "I will never ask for them. How else can I help?")


@dataclass
class InputDecision:
    text: str                       # what the model will see (possibly redacted)
    events: list[GuardrailEvent]
    blocked_reply: str | None = None


def check_input(text: str) -> InputDecision:
    cfg = policy()["input"]
    events: list[GuardrailEvent] = []
    if len(text) > cfg["max_chars"]:
        text = text[: cfg["max_chars"]]
        events.append(GuardrailEvent("input", "max_length", "info", {"max_chars": cfg["max_chars"]}))
    if cfg.get("block_secrets") and _SECRET.search(text):
        events.append(GuardrailEvent("input", "secret_shared", "block", {}))
        clean, _ = redact(_SECRET.sub("[secret removed]", text), cfg["redact"])
        return InputDecision(clean, events, SECRET_REPLY)
    clean, found = redact(text, cfg["redact"])
    for kind in sorted(set(found)):
        events.append(GuardrailEvent("input", f"redacted_{kind}", "warn", {}))
    injection = _INJECTION.search(clean) if cfg.get("prompt_injection") else None
    if injection:
        events.append(GuardrailEvent("input", "prompt_injection", "warn", {"excerpt": injection.group(0)}))
    return InputDecision(clean, events)


# --------------------------------------------------------------------------------------------------------
# Output grounding
# --------------------------------------------------------------------------------------------------------

_AMOUNT = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|crore|cr)?", re.I)
_LAKH_WORDS = re.compile(r"(\d+(?:\.\d+)?)\s*(lakh|lac|crore)\b", re.I)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_ACCOUNT_ID = re.compile(r"\b(?:HL|PL|LP|CC)\d{7}\b|\bSR\d{7}\b")


def _numbers_in(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", obj):
            try:
                out.add(float(m.group(0).replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)


class Grounding:
    """Everything the tools returned in this conversation: the evidence a reply may cite."""

    def __init__(self):
        self.numbers: set[float] = set()
        self.ids: set[str] = set()

    def add(self, data: Any) -> None:
        _numbers_in(data, self.numbers)
        self.ids |= set(_ACCOUNT_ID.findall(json.dumps(data, default=str)))

    def has(self, value: float, tolerance_pct: float, absolute: float = 0.0) -> bool:
        if value in self.numbers:
            return True
        tol = max(1.0, abs(value) * tolerance_pct / 100, absolute)
        return any(abs(value - n) <= tol for n in self.numbers)


def _rounding_tolerance(number: str, scale: float) -> float:
    """'1.6 lakh' is a rounded figure: accept anything that rounds to it (half of the last stated digit)."""
    decimals = len(number.split(".")[1]) if "." in number else 0
    return 0.5 * 10 ** -decimals * scale


def check_output(reply: str, grounding: Grounding) -> list[GuardrailEvent]:
    cfg = policy()["output"]
    if not cfg.get("grounding_check"):
        return []
    tol = cfg["tolerance_pct"]
    unverified: list[str] = []
    for m in _AMOUNT.finditer(reply):
        number, unit = m.group(1).replace(",", ""), (m.group(2) or "").lower()
        scale = 1e5 if unit in ("lakh", "lac") else 1e7 if unit in ("crore", "cr") else 1
        value = float(number) * scale
        if value and not grounding.has(value, tol, _rounding_tolerance(number, scale) if scale > 1 else 0):
            unverified.append(m.group(0).strip())
    for m in _LAKH_WORDS.finditer(reply):
        if _AMOUNT.search(reply[max(0, m.start() - 4):m.end()]):
            continue  # already checked with its currency marker
        scale = 1e7 if m.group(2).lower() == "crore" else 1e5
        if not grounding.has(float(m.group(1)) * scale, tol, _rounding_tolerance(m.group(1), scale)):
            unverified.append(m.group(0))
    for m in _PERCENT.finditer(reply):
        if not grounding.has(float(m.group(1)), 0):
            unverified.append(m.group(0))
    unknown_ids = sorted(set(_ACCOUNT_ID.findall(reply)) - grounding.ids)
    events = []
    if unverified:
        events.append(GuardrailEvent("output", "ungrounded_figures", "warn", {"values": unverified[:10]}))
    if unknown_ids:
        events.append(GuardrailEvent("output", "unknown_account_ids", "warn", {"ids": unknown_ids}))
    return events


# --------------------------------------------------------------------------------------------------------
# Actions and channels
# --------------------------------------------------------------------------------------------------------

class PolicyViolation(Exception):
    def __init__(self, event: GuardrailEvent, message: str):
        super().__init__(message)
        self.event, self.message = event, message


def check_action(tool: str, args: dict, channel: str, executed_so_far: int) -> None:
    """Raise PolicyViolation if this action may not be proposed. Called by the engine, not the model."""
    cfg = policy()["actions"]
    if tool in cfg["disabled_tools"]:
        raise PolicyViolation(GuardrailEvent("action", "tool_disabled", "block", {"tool": tool}),
                              "This service is temporarily unavailable. I can connect you to our team.")
    if channel == "voice" and tool in cfg["disallowed_on_voice"]:
        raise PolicyViolation(GuardrailEvent("action", "not_allowed_on_voice", "block", {"tool": tool}),
                              "For security this can only be done in the app or website, not on a call.")
    if executed_so_far >= cfg["max_executed_per_session"]:
        raise PolicyViolation(GuardrailEvent("action", "session_action_limit", "block",
                                             {"limit": cfg["max_executed_per_session"]}),
                              "I've reached the number of requests I can submit in one conversation. "
                              "Our team can help with anything further.")
    limit = cfg["limits"].get(tool)
    if limit and float(args.get(limit["field"]) or 0) > limit["max"]:
        raise PolicyViolation(GuardrailEvent("action", "amount_limit", "block",
                                             {"tool": tool, "value": args.get(limit["field"]), "max": limit["max"]}),
                              limit["reason"])


def check_model(model: str) -> None:
    allowed = policy()["models"]["allowed"]
    if model not in allowed:
        raise RuntimeError(f"Model {model!r} is not in the governance allow-list {allowed}")
