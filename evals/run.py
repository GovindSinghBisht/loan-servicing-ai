"""Run the evaluation conversations against the live mock systems and the real model.

    uv run python -m mock_systems.run                 # in another terminal
    uv run python -m evals.run [--only emi-increase-why] [--chaos off] [--concurrency 3]

Writes evals/results/<timestamp>.json and prints a scorecard plus latency percentiles. Cases with a
`chaos` block change fault injection on the shared mock systems, so they run on their own after the rest.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from engine.agent import Agent  # noqa: E402
from engine.clients import SYSTEMS, Systems  # noqa: E402
from engine.core import Session  # noqa: E402
import engine.tools  # noqa: E402,F401

from .cases import CASES  # noqa: E402

RESULTS = Path(__file__).parent / "results"


def norm(s: str) -> str:
    s = s.lower().replace("₹", "").replace("rs.", "").replace("rs ", "")
    return re.sub(r"(?<=\d),(?=\d)", "", s)


def grade(turn: dict, r) -> list[str]:
    text = norm(r.text)
    problems = []
    for group in turn.get("include", []):
        if not any(norm(alt) in text for alt in group):
            problems.append(f"missing one of {group}")
    for bad in turn.get("exclude", []):
        if norm(bad) in text:
            problems.append(f"contains '{bad}'")
    called = {c["name"] for c in r.tool_calls}
    for t in turn.get("tools", []):
        if t not in called:
            problems.append(f"did not call {t}")
    for t in turn.get("no_tools", []):
        if any(c["name"] == t and c["ok"] and c["status"] != "CONFIRMATION_REQUIRED" for c in r.tool_calls):
            problems.append(f"called {t}")
    executed = any(c["name"] == "confirm_action" and c["ok"] for c in r.tool_calls)
    if turn.get("confirmed") is True and not executed:
        problems.append("expected an action to execute")
    if turn.get("confirmed") is False and executed:
        problems.append("executed an action without confirmation")
    return problems


async def put_chaos(system: str, fields: dict) -> None:
    async with httpx.AsyncClient() as http:
        await http.put(SYSTEMS[system].base_url + "/admin/chaos", json=fields)


DEFAULTS: dict[str, dict] = {}


async def run_case(case: dict, systems: Systems) -> dict:
    customer = await systems.crm.get("/customers/lookup", phone=case["phone"])
    agent = Agent(Session(customer_id=customer["customer_id"], systems=systems))
    await agent.start()
    turns = []
    for turn in case["turns"]:
        try:
            r = await agent.respond(turn["user"])
            problems = grade(turn, r)
            turns.append({"user": turn["user"], "reply": r.text, "problems": problems,
                          "tools": r.tool_calls, "metrics": asdict(r.metrics),
                          "spans": [asdict(s) for s in r.spans]})
        except Exception as e:  # a crash is a failed turn, not a failed run
            turns.append({"user": turn["user"], "reply": "", "problems": [f"error: {type(e).__name__}: {e}"],
                          "tools": [], "metrics": {}, "spans": []})
    return {"id": case["id"], "passed": all(not t["problems"] for t in turns), "turns": turns,
            "provider": agent.provider.name, "model": agent.provider.model}


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))], 1)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--chaos", choices=["on", "off"], default="on", help="background fault injection for normal cases")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or c["id"] in args.only]
    async with httpx.AsyncClient() as http:
        for name, cfg in SYSTEMS.items():
            DEFAULTS[name] = (await http.get(cfg.base_url + "/admin/chaos")).json()
            await http.put(cfg.base_url + "/admin/chaos", json={"enabled": args.chaos == "on"})

    systems = Systems()
    sem = asyncio.Semaphore(args.concurrency)
    started = time.time()

    async def guarded(c):
        async with sem:
            out = await run_case(c, systems)
            print(f"  {'PASS' if out['passed'] else 'FAIL'}  {c['id']}")
            return out

    normal = [c for c in cases if not c.get("chaos")]
    results = list(await asyncio.gather(*(guarded(c) for c in normal)))
    for c in [c for c in cases if c.get("chaos")]:
        for system, fields in c["chaos"].items():
            await put_chaos(system, {**fields, "enabled": True})
        try:
            results.append(await guarded(c))
        finally:
            for system in c["chaos"]:
                await put_chaos(system, {**DEFAULTS[system], "enabled": args.chaos == "on"})
    await systems.close()
    for name in SYSTEMS:
        await put_chaos(name, DEFAULTS[name])  # leave the mock systems as we found them

    turns = [t for r in results for t in r["turns"] if t["metrics"]]
    first = [t["metrics"]["first_text_ms"] for t in turns if t["metrics"].get("first_text_ms")]
    total = [t["metrics"]["total_ms"] for t in turns]
    per_system = defaultdict(list)
    for t in turns:
        for s in t["spans"]:
            if s["outcome"] in ("ok", "not_found"):
                per_system[s["system"]].append(s["ms"])
    summary = {
        "cases": len(results), "passed": sum(r["passed"] for r in results),
        "turns": len(turns), "turns_passed": sum(not t["problems"] for r in results for t in r["turns"]),
        "latency_ms": {
            "first_token": {"p50": pct(first, 50), "p95": pct(first, 95)},
            "turn_total": {"p50": pct(total, 50), "p95": pct(total, 95)},
            "model_per_turn": {"p50": pct([t["metrics"]["llm_ms"] for t in turns], 50)},
            "tools_per_turn": {"p50": pct([t["metrics"]["tools_ms"] for t in turns], 50)},
            "backend_calls": {k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95)} for k, v in sorted(per_system.items())},
        },
        "tokens": {"input": sum(t["metrics"]["input_tokens"] for t in turns),
                   "output": sum(t["metrics"]["output_tokens"] for t in turns),
                   "cached": sum(t["metrics"]["cached_tokens"] for t in turns)},
        "provider": results[0]["provider"] if results else None, "model": results[0]["model"] if results else None,
        "chaos": args.chaos, "wall_s": round(time.time() - started, 1),
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=1, default=str))

    print(f"\n{summary['passed']}/{summary['cases']} conversations passed "
          f"({summary['turns_passed']}/{summary['turns']} turns)")
    for r in results:
        for t in r["turns"]:
            for p in t["problems"]:
                print(f"  {r['id']}: {p}\n      reply: {t['reply'][:220]!r}")
    print(json.dumps(summary["latency_ms"], indent=1))
    print(f"results: {out}")


if __name__ == "__main__":
    asyncio.run(main())
