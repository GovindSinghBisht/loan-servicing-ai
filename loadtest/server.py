"""The real API with a latency-calibrated stand-in for the LLM, for load testing our own stack.

    uv run python -m mock_systems.run          # terminal 1 (fault injection on)
    uv run python -m loadtest.server           # terminal 2, API on :8000
    uv run python -m loadtest.run              # terminal 3

Everything except the model is real: auth and sessions, guardrails, the agent loop, tools, the resilient
clients, the mock systems with their latency and failures, storage and SSE streaming. The model is the
offline keyword router, delayed per step by a log-normal sample calibrated to the real model's measured
latency (evals: p50 1.2 s, p95 1.5 s per step), and streaming its reply word by word. That measures our
capacity without hitting the LLM vendor's rate limits; vendor throughput is a separate, contractual limit.

Storage goes to a separate SQLite file and tracing is off, so a load run never touches the real database
or Langfuse.
"""
from __future__ import annotations

import asyncio
import math
import os
import random

# Set before the server loads .env: load_dotenv never overrides a variable that is already set.
os.environ["DATABASE_URL"] = os.environ.get("LOADTEST_DATABASE_URL", "sqlite+aiosqlite:///data/loadtest.db")
os.environ["LANGFUSE_PUBLIC_KEY"] = os.environ["LANGFUSE_SECRET_KEY"] = ""

import uvicorn  # noqa: E402

from engine import server  # noqa: E402
from tests.offline_llm import OfflineProvider  # noqa: E402

STEP_P50_MS = float(os.environ.get("SIM_LLM_STEP_P50_MS", 1200))
STEP_P95_MS = float(os.environ.get("SIM_LLM_STEP_P95_MS", 1500))
WORD_MS = float(os.environ.get("SIM_LLM_WORD_MS", 15))  # streaming pace once text starts
_rng = random.Random()


def _step_delay_s() -> float:
    sigma = max(math.log(STEP_P95_MS / STEP_P50_MS) / 1.645, 0.01)
    return _rng.lognormvariate(math.log(STEP_P50_MS), sigma) / 1000


class SimulatedLLM(OfflineProvider):
    """Same decisions as the offline router, with the real model's timing."""

    async def step(self, prompt, history, on_text=None):
        delay = _step_delay_s()
        words: list[str] = []
        step = await super().step(prompt, history, words.append)  # decide first, then pace the output
        if step.tool_calls:
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(delay * 0.6)  # time to first token; the rest streams
            for w in words:
                if on_text:
                    on_text(w)
                await asyncio.sleep(WORD_MS / 1000)
        return step


server.provider_factory = SimulatedLLM

if __name__ == "__main__":
    uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), log_level="warning",
                backlog=4096, timeout_keep_alive=30)
