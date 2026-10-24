"""Resilient clients for the enterprise systems.

Every call gets a per-system timeout, bounded retries with jittered backoff (GETs always; writes only
because they carry an Idempotency-Key), a circuit breaker so a dead system fails fast instead of
stalling the conversation, optional TTL caching for slow-changing data, and a latency span recorded
into the current trace.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

API_KEY = os.environ.get("MOCK_API_KEY", "dev-key")


@dataclass
class SystemConfig:
    base_url: str
    timeout_s: float
    retries: int = 2
    cache_ttl_s: float = 0  # 0 = no caching


SYSTEMS: dict[str, SystemConfig] = {
    "crm": SystemConfig(os.environ.get("CRM_URL", "http://127.0.0.1:8101"), 1.5),
    "loans": SystemConfig(os.environ.get("LOANS_URL", "http://127.0.0.1:8102"), 2.0),
    "payments": SystemConfig(os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8103"), 3.0),
    "policy": SystemConfig(os.environ.get("POLICY_URL", "http://127.0.0.1:8104"), 1.0, cache_ttl_s=300),
    "service_ops": SystemConfig(os.environ.get("SERVICE_OPS_URL", "http://127.0.0.1:8105"), 2.5),
    "cards": SystemConfig(os.environ.get("CARDS_URL", "http://127.0.0.1:8106"), 2.0),
}


class SystemUnavailable(Exception):
    def __init__(self, system: str, reason: str):
        super().__init__(f"{system} unavailable: {reason}")
        self.system, self.reason = system, reason


class NotFound(Exception):
    def __init__(self, system: str, path: str):
        super().__init__(f"{system}: {path} not found")
        self.system, self.path = system, path


@dataclass
class Span:
    system: str
    method: str
    path: str
    ms: float
    attempts: int
    outcome: str  # ok | cached | not_found | unavailable | circuit_open


_trace: contextvars.ContextVar[list[Span] | None] = contextvars.ContextVar("trace", default=None)


def start_trace() -> list[Span]:
    spans: list[Span] = []
    _trace.set(spans)
    return spans


def _record(span: Span) -> None:
    spans = _trace.get()
    if spans is not None:
        spans.append(span)


@dataclass
class CircuitBreaker:
    threshold: int = 5
    cooldown_s: float = 15
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return True  # half-open: let one probe through
        return False

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


@dataclass
class SystemClient:
    name: str
    config: SystemConfig
    http: httpx.AsyncClient
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    cache: dict[str, tuple[float, Any]] = field(default_factory=dict)

    async def get(self, path: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        key = f"{path}?{sorted(params.items())}"
        if self.config.cache_ttl_s and key in self.cache:
            at, value = self.cache[key]
            if time.monotonic() - at < self.config.cache_ttl_s:
                _record(Span(self.name, "GET", path, 0.0, 0, "cached"))
                return value
        value = await self._request("GET", path, params=params)
        if self.config.cache_ttl_s:
            self.cache[key] = (time.monotonic(), value)
        return value

    async def get_bytes(self, path: str, **params) -> tuple[bytes, str]:
        """Fetch a binary document (e.g. a PDF). Same timeout and circuit breaker; no retries for large bodies."""
        if not self.breaker.allow():
            raise SystemUnavailable(self.name, "circuit open after repeated failures")
        started = time.perf_counter()
        try:
            r = await self.http.get(self.config.base_url + path, params=params, timeout=self.config.timeout_s * 2,
                                    headers={"x-api-key": API_KEY})
        except httpx.TransportError as e:
            self.breaker.failure()
            raise SystemUnavailable(self.name, type(e).__name__)
        if r.status_code == 404:
            raise NotFound(self.name, path)
        if r.status_code >= 500:
            self.breaker.failure()
            raise SystemUnavailable(self.name, f"HTTP {r.status_code}")
        self.breaker.success()
        _record(Span(self.name, "GET", path, _ms(started), 1, "ok"))
        return r.content, r.headers.get("content-type", "application/octet-stream")

    async def post(self, path: str, body: dict, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return await self._request("POST", path, json=body, headers=headers)

    async def _request(self, method: str, path: str, **kw) -> Any:
        started = time.perf_counter()
        if not self.breaker.allow():
            _record(Span(self.name, method, path, 0.0, 0, "circuit_open"))
            raise SystemUnavailable(self.name, "circuit open after repeated failures")
        last = "unknown"
        headers = {"x-api-key": API_KEY, **kw.pop("headers", {})}  # same Idempotency-Key on every retry
        for attempt in range(1, self.config.retries + 2):
            try:
                r = await self.http.request(method, self.config.base_url + path, timeout=self.config.timeout_s,
                                            headers=headers, **kw)
                if r.status_code == 404:
                    self.breaker.success()
                    _record(Span(self.name, method, path, _ms(started), attempt, "not_found"))
                    raise NotFound(self.name, path)
                if r.status_code < 500:
                    r.raise_for_status()
                    self.breaker.success()
                    _record(Span(self.name, method, path, _ms(started), attempt, "ok"))
                    return r.json()
                last = f"HTTP {r.status_code}"
            except httpx.TimeoutException:
                last = f"timeout after {self.config.timeout_s}s"
            except httpx.TransportError as e:
                last = f"connection error: {type(e).__name__}"
            self.breaker.failure()
            if attempt <= self.config.retries:
                await asyncio.sleep(0.05 * 2 ** attempt + random.uniform(0, 0.05))
        _record(Span(self.name, method, path, _ms(started), self.config.retries + 1, "unavailable"))
        raise SystemUnavailable(self.name, last)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class Systems:
    """One shared client per enterprise system. Pass `transport` to run against in-process apps in tests."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(transport=transport, limits=httpx.Limits(max_connections=200))
        for name, cfg in SYSTEMS.items():
            setattr(self, name, SystemClient(name, cfg, self.http))
        self._business_date: str | None = None

    crm: SystemClient
    loans: SystemClient
    payments: SystemClient
    policy: SystemClient
    service_ops: SystemClient
    cards: SystemClient

    async def business_date(self) -> str:
        if self._business_date is None:
            r = await self.http.get(SYSTEMS["policy"].base_url + "/health", timeout=2)
            self._business_date = r.json()["business_date"]
        return self._business_date

    async def close(self) -> None:
        await self.http.aclose()
