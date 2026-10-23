"""Talk to the assistant in the terminal.

    uv run python -m mock_systems.run          # in one terminal
    uv run python -m engine.cli --phone +919800000001

Prints the reply as it streams, then a latency line: time to first token, model vs tool time, and
the slowest backend calls.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from .agent import Agent  # noqa: E402
from .clients import NotFound, Systems  # noqa: E402
from .core import Session, execute  # noqa: E402
from .packs import packs_for  # noqa: E402
from .storage import Store  # noqa: E402
import engine.tools  # noqa: E402,F401

DIM, CYAN, RESET = "\033[2m", "\033[36m", "\033[0m"


def latency_line(r) -> str:
    m = r.metrics
    calls = sorted((s for s in r.spans if s.outcome != "cached"), key=lambda s: -s.ms)[:3]
    slow = ", ".join(f"{s.system} {s.ms:.0f}ms" + (f" x{s.attempts}" if s.attempts > 1 else "") for s in calls)
    first = f"{m.first_text_ms:.0f}ms" if m.first_text_ms else "-"
    return (f"{DIM}first token {first} · total {m.total_ms:.0f}ms · model {m.llm_ms:.0f}ms · tools {m.tools_ms:.0f}ms"
            f" · {m.steps} step(s){' · slowest: ' + slow if slow else ''}{RESET}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="+919800000001", help="registered mobile of a demo customer")
    ap.add_argument("--pack", default=None, help="comma-separated packs; default: from the customer's products")
    args = ap.parse_args()

    systems = Systems()
    try:
        customer = await systems.crm.get("/customers/lookup", phone=args.phone)
    except NotFound:
        sys.exit(f"No customer with phone {args.phone}")
    print(f"{DIM}OTP verified (demo) · {customer['first_name']} {customer['last_name']} · {customer['customer_id']}{RESET}")

    store = Store()
    await store.init()
    session = Session(customer_id=customer["customer_id"], systems=systems, store=store)
    overview = await execute(session, "get_customer_overview", {})
    agent = Agent(session, args.pack.split(",") if args.pack else packs_for(overview.get("data") or {}))
    await agent.start(overview)
    await store.start_conversation(session.session_id, session.customer_id, "text", sorted(agent.pack.tool_groups),
                                   agent.provider.name, agent.provider.model)
    print("Type a question, or 'quit'.\n")
    try:
        while True:
            try:
                text = input(f"{CYAN}you>{RESET} ").strip()
            except EOFError:
                break
            if text.lower() in ("quit", "exit"):
                break
            if not text:
                continue
            print("assistant> ", end="", flush=True)
            r = await agent.respond(
                text,
                on_text=lambda t: print(t, end="", flush=True),
                on_tool=lambda name, _: print(f"{DIM}[{name}]{RESET} ", end="", flush=True),
            )
            print("\n" + latency_line(r) + "\n")
    finally:
        await store.close()
        await systems.close()


if __name__ == "__main__":
    asyncio.run(main())
