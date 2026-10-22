"""Shared plumbing for the mock enterprise systems.

Each system is a separate FastAPI app that owns its own data file. Every request (except /health and
/admin/*) passes through fault injection: log-normal latency calibrated from p50/p95, plus random 503s
and hangs. Profiles can be changed at runtime via PUT /admin/chaos, so a demo can make one system slow
or flaky on cue, and set globally with CHAOS=off for deterministic tests.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data" / "generated"))
POLICY_DIR = ROOT / "data" / "policies"
API_KEY = os.environ.get("MOCK_API_KEY", "dev-key")

PORTS = {"crm": 8101, "loans": 8102, "payments": 8103, "policy": 8104, "service_ops": 8105, "cards": 8106}


def load(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text())


def business_date() -> date:
    """The systems' notion of "today": the date the data was generated for, unless overridden."""
    if v := os.environ.get("BUSINESS_DATE"):
        return date.fromisoformat(v)
    return date.fromisoformat(load("manifest")["as_of"])


def now_iso() -> str:
    """Business date combined with the current wall-clock time, in IST."""
    return f"{business_date().isoformat()}T{datetime.now().strftime('%H:%M:%S')}+05:30"


@dataclass
class Chaos:
    p50_ms: float
    p95_ms: float
    error_rate: float   # fraction of requests answered with 503
    hang_rate: float    # fraction of requests that stall for hang_ms (exercises client timeouts)
    hang_ms: float = 8000
    enabled: bool = True

    def sample_latency_ms(self, rng: random.Random) -> float:
        if self.p50_ms <= 0:
            return 0.0
        sigma = max(math.log(self.p95_ms / self.p50_ms) / 1.645, 0.01)
        return rng.lognormvariate(math.log(self.p50_ms), sigma)


DEFAULT_CHAOS = {
    "crm": Chaos(60, 180, 0.01, 0.002),
    "loans": Chaos(120, 400, 0.02, 0.005),
    "payments": Chaos(350, 1200, 0.04, 0.01),   # the slow legacy system
    "policy": Chaos(40, 120, 0.005, 0.0),
    "service_ops": Chaos(200, 700, 0.03, 0.005),
    "cards": Chaos(150, 500, 0.02, 0.005),
}


def make_app(name: str) -> FastAPI:
    app = FastAPI(title=f"Mock {name}", docs_url="/docs")
    chaos = Chaos(**asdict(DEFAULT_CHAOS[name]))
    chaos.enabled = os.environ.get("CHAOS", "on").lower() != "off"
    rng = random.Random()
    idempotency: dict[str, Any] = {}
    app.state.chaos = chaos
    app.state.idempotency = idempotency

    @app.middleware("http")
    async def fault_injection(request: Request, call_next):
        path = request.url.path
        if path in ("/health", "/docs", "/openapi.json") or path.startswith("/admin"):
            return await call_next(request)
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        started = time.perf_counter()
        if chaos.enabled:
            await asyncio.sleep(chaos.sample_latency_ms(rng) / 1000)
            roll = rng.random()
            if roll < chaos.hang_rate:
                await asyncio.sleep(chaos.hang_ms / 1000)
            elif roll < chaos.hang_rate + chaos.error_rate:
                return JSONResponse({"error": "SERVICE_UNAVAILABLE", "system": name}, status_code=503)
        response = await call_next(request)
        response.headers["x-service-time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    @app.get("/health")
    def health():
        return {"system": name, "status": "ok", "business_date": business_date().isoformat()}

    @app.get("/admin/chaos")
    def get_chaos():
        return asdict(chaos)

    @app.put("/admin/chaos")
    def set_chaos(patch: dict):
        for k, v in patch.items():
            if not hasattr(chaos, k):
                raise HTTPException(400, f"unknown chaos field {k}")
            setattr(chaos, k, type(getattr(chaos, k))(v))
        return asdict(chaos)

    return app


def idempotent(app: FastAPI, request: Request, build) -> Any:
    """Return the stored response for a repeated Idempotency-Key instead of performing the write twice."""
    key = request.headers.get("idempotency-key")
    if key and key in app.state.idempotency:
        return app.state.idempotency[key]
    result = build()
    if key:
        app.state.idempotency[key] = result
    return result


def not_found(kind: str, ident: str) -> HTTPException:
    return HTTPException(404, {"error": "NOT_FOUND", "entity": kind, "id": ident})
