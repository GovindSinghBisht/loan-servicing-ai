"""Concurrent-conversation load test against the running API (see loadtest/server.py).

    uv run python -m loadtest.run [--levels 50 200 500 1000] [--turns 3] [--think 2.0] [--ramp 10]
    uv run python -m loadtest.run --api http://127.0.0.1:8001 http://127.0.0.1:8002 --procs 4   # several instances

Each virtual customer signs in (OTP flow, snapshot from four systems), holds a multi-turn conversation over
SSE and signs out. With several --api instances each conversation stays on the instance it signed in to,
as a load balancer with session affinity would route it. A level of N means N conversations open at the same time. Customers are drawn from all
synthetic profiles, so the tools hit different loans, payments and cards. Writes loadtest/results/<ts>.json
and prints one row per level.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx

APIS = [os.environ.get("API_URL", "http://127.0.0.1:8000")]
ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "results"
DEMO_OTP = "123456"

LOAN_SCRIPT = ["Why did my EMI change?", "Show me the payments that failed recently.", "What if I prepay 50,000?",
               "What's the status of my service requests?", "What are my options on this loan?"]
CARD_SCRIPT = ["Why was I charged a fee on my card?", "Show my card details.", "What's the status of my requests?"]


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))], 1)


def phones() -> list[str]:
    crm = json.loads((ROOT / "data" / "generated" / "crm.json").read_text())
    return [c["phone"].replace("-", "") for c in crm["customers"]]


FAILOVER = os.environ.get("LOADTEST_FAILOVER") == "1"
# Connection refused, or the stream cut off: what a client sees when its instance dies.
CRASH = (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


async def conversation(http: httpx.AsyncClient, apis: list[str], api: str, phone: str, turns: int, think: float,
                       rec: dict) -> None:
    t0 = time.perf_counter()
    for _ in range(len(apis) if FAILOVER else 1):
        try:
            r = await http.post(f"{api}/api/login", json={"phone": phone, "otp": DEMO_OTP})
            break
        except CRASH as e:
            if not FAILOVER:
                rec["errors"][f"login:{type(e).__name__}"] += 1
                return
            rec["failover_reasons"][f"login:{type(e).__name__}"] += 1
            api = apis[(apis.index(api) + 1) % len(apis)]  # the load balancer sends it elsewhere
        except httpx.HTTPError as e:
            rec["errors"][f"login:{type(e).__name__}"] += 1
            return
    else:
        rec["errors"]["login:no_instance"] += 1
        return
    if r.status_code != 200:
        rec["errors"][f"login:{r.status_code}"] += 1
        return
    rec["login_ms"].append((time.perf_counter() - t0) * 1000)
    body = r.json()
    headers = {"Authorization": f"Bearer {body['token']}"}
    script = LOAN_SCRIPT if "loans" in body["packs"] else CARD_SCRIPT
    start = random.randrange(len(script))
    for i in range(turns):
        await asyncio.sleep(think * random.uniform(0.5, 1.5))  # the customer reads, then asks the next thing
        text = script[(start + i) % len(script)]
        if await turn(http, api, headers, text, rec) == "crash":
            # Another instance takes the same token, restores the session from the database and answers.
            api = apis[(apis.index(api) + 1) % len(apis)]
            await turn(http, api, headers, text, rec, after_failover=True)
    try:
        await http.post(f"{api}/api/logout", headers=headers)
    except httpx.HTTPError:
        pass


async def turn(http: httpx.AsyncClient, api: str, headers: dict, text: str, rec: dict,
               after_failover: bool = False) -> str:
    t0 = time.perf_counter()
    first_event = first_text = None
    event = None
    try:
        async with http.stream("POST", f"{api}/api/messages", headers=headers, json={"text": text}) as r:
            if r.status_code != 200:
                rec["errors"][f"message:{r.status_code}"] += 1
                return "error"
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                    if first_event is None:
                        first_event = time.perf_counter()
                elif line.startswith("data: "):
                    if event == "text" and first_text is None:
                        first_text = time.perf_counter()
                    elif event == "error":
                        rec["errors"]["turn:error_event"] += 1
                        return "error"
                    elif event == "done":
                        done = json.loads(line[6:])
                        for system, b in done.get("backend", {}).items():
                            rec["backend_calls"][system] += b["calls"]
                            rec["backend_retries"][system] += b["retries"]
                            rec["backend_failed"][system] += b["failed"]
                        rec["server_first_text_ms"].append(done["metrics"].get("first_text_ms") or 0)
                        rec["server_tools_ms"].append(done["metrics"].get("tools_ms") or 0)
    except CRASH as e:
        if FAILOVER and not after_failover:
            rec["failover_reasons"][f"message:{type(e).__name__}"] += 1
            return "crash"
        rec["errors"][f"message:{type(e).__name__}"] += 1
        return "error"
    except httpx.HTTPError as e:
        rec["errors"][f"message:{type(e).__name__}"] += 1
        return "error"
    end = time.perf_counter()
    rec["turn_ms"].append((end - t0) * 1000)
    if first_event:
        rec["first_event_ms"].append((first_event - t0) * 1000)
    if first_text:
        rec["first_text_ms"].append((first_text - t0) * 1000)
    if after_failover:
        rec["failover_turn_ms"].append((end - t0) * 1000)
    return "ok"


def _new_rec() -> dict:
    return {"login_ms": [], "turn_ms": [], "first_event_ms": [], "first_text_ms": [], "server_first_text_ms": [],
            "server_tools_ms": [], "errors": Counter(), "backend_calls": Counter(), "backend_retries": Counter(),
            "backend_failed": Counter(), "failover_reasons": Counter(), "failover_turn_ms": []}


async def drive(n: int, indices: list[int], turns: int, think: float, ramp: float, pool: list[str],
                apis: list[str]) -> dict:
    """Run the conversations with these indices (out of n in the level) and return the raw samples."""
    rec = _new_rec()
    size = len(indices) * 2 + 50
    limits = httpx.Limits(max_connections=size, max_keepalive_connections=size)
    async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120, connect=30)) as http:
        async def one(i: int):
            await asyncio.sleep(ramp * i / n)  # spread sign-ins over the ramp
            await conversation(http, apis, apis[i % len(apis)], pool[i % len(pool)], turns, think, rec)

        await asyncio.gather(*(one(i) for i in indices))
    return rec


def _drive_in_process(job: tuple) -> dict:
    rec = asyncio.run(drive(*job))
    return {k: dict(v) if isinstance(v, Counter) else v for k, v in rec.items()}


async def level(n: int, turns: int, think: float, ramp: float, pool: list[str], procs: int) -> dict:
    """N concurrent conversations. With procs > 1 the load is generated from several processes, so the
    generator's own CPU (parsing N event streams) doesn't distort the measurement."""
    started = time.perf_counter()
    if procs == 1:
        parts = [await drive(n, list(range(n)), turns, think, ramp, pool, APIS)]
    else:
        jobs = [(n, list(range(k, n, procs)), turns, think, ramp, pool, list(APIS)) for k in range(procs)]
        with ProcessPoolExecutor(procs) as ex:
            parts = await asyncio.gather(*(asyncio.wrap_future(ex.submit(_drive_in_process, j)) for j in jobs))
    wall = time.perf_counter() - started
    rec = _new_rec()
    for part in parts:
        for k, v in part.items():
            if isinstance(rec[k], Counter):
                rec[k].update(v)
            else:
                rec[k] += v

    def p(key: str, *ps: int) -> dict:
        return {f"p{x}": pct(rec[key], x) for x in ps}

    ok_turns = len(rec["turn_ms"])
    attempted = n * turns
    calls = sum(rec["backend_calls"].values())
    return {
        "concurrent_conversations": n, "turns_attempted": attempted, "turns_ok": ok_turns,
        "success_pct": round(100 * ok_turns / attempted, 2) if attempted else None,
        "errors": dict(rec["errors"]), "wall_s": round(wall, 1), "turns_per_s": round(ok_turns / wall, 1),
        "latency_ms": {
            "login": p("login_ms", 50, 95, 99),
            "first_event": p("first_event_ms", 50, 95),
            "first_text": p("first_text_ms", 50, 95, 99),
            "turn_total": p("turn_ms", 50, 95, 99),
            "server_first_text": p("server_first_text_ms", 50, 95, 99),
            "server_tools_per_turn": p("server_tools_ms", 50, 95),
        },
        "backend": {s: {"calls": rec["backend_calls"][s], "retries": rec["backend_retries"][s],
                        "failed": rec["backend_failed"][s]} for s in sorted(rec["backend_calls"])},
        "backend_failed_pct": round(100 * sum(rec["backend_failed"].values()) / calls, 2) if calls else None,
        "failover": {"moved": sum(rec["failover_reasons"].values()), "reasons": dict(rec["failover_reasons"]),
                     "restored_turn_ms": p("failover_turn_ms", 50, 95, 99)},
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, nargs="+", default=[50, 200, 500, 1000])
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--think", type=float, default=2.0, help="seconds a customer pauses between turns (avg)")
    ap.add_argument("--ramp", type=float, default=10.0, help="seconds over which sign-ins are spread")
    ap.add_argument("--api", nargs="+", default=APIS, help="API instances (conversations are pinned to one)")
    ap.add_argument("--procs", type=int, default=1, help="load-generator processes")
    ap.add_argument("--failover", action="store_true",
                    help="when an instance is unreachable, move the conversation to the next one (as a load balancer would)")
    args = ap.parse_args()
    APIS[:] = args.api
    if args.failover:  # read at import by the generator processes too
        os.environ["LOADTEST_FAILOVER"] = "1"
        global FAILOVER
        FAILOVER = True
    async with httpx.AsyncClient() as http:
        health = (await http.get(f"{APIS[0]}/api/health")).json()
    pool = phones()
    random.shuffle(pool)
    print(f"API {' '.join(APIS)} · storage {health.get('storage')} · tracing {health.get('tracing')} · "
          f"{len(pool)} customers · {args.procs} generator process(es)")
    print(f"{'conv':>6} {'ok%':>7} {'turn/s':>7} {'login p50/p95':>15} {'1st text p50/p95':>18} "
          f"{'server 1st text p50/p95':>24} {'turn p50/p95/p99':>22} {'tools p50':>10} {'backend fail%':>14}")
    runs = []
    for n in args.levels:
        r = await level(n, args.turns, args.think, args.ramp, pool, args.procs)
        runs.append(r)
        L = r["latency_ms"]
        print(f"{n:>6} {r['success_pct']:>7} {r['turns_per_s']:>7} "
              f"{L['login']['p50']:>7}/{L['login']['p95']:<7} {L['first_text']['p50']:>8}/{L['first_text']['p95']:<9} "
              f"{L['server_first_text']['p50']:>12}/{L['server_first_text']['p95']:<11} "
              f"{L['turn_total']['p50']:>7}/{L['turn_total']['p95']}/{L['turn_total']['p99']:<6} "
              f"{L['server_tools_per_turn']['p50']:>10} {r['backend_failed_pct']:>14}"
              + (f"  errors {r['errors']}" if r["errors"] else ""))
        if r["failover"]["moved"]:
            print(f"       failover: {r['failover']['moved']} requests moved {r['failover']['reasons']}; "
                  f"restored turns {r['failover']['restored_turn_ms']}")
        await asyncio.sleep(3)  # let the write-behind queue drain between levels
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"args": vars(args), "health": health, "runs": runs}, indent=1))
    print(f"results: {out}")


if __name__ == "__main__":
    asyncio.run(main())
