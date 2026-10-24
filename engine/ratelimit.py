"""Sliding-window rate limits for the public demo: sign-ins per IP, messages per session and per IP, speech.

Limits come from the governance policy (`rate_limits`, per minute). State is per process, which suits one
instance; several instances would share a counter store (e.g. Redis) instead.
"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int) -> float | None:
        """Record a hit and return None if allowed, or the seconds until the next request would be."""
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= limit:
            return round(self.window_s - (now - hits[0]), 1)
        hits.append(now)
        if len(self._hits) > 50_000:  # drop idle keys so memory stays bounded
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window_s}
        return None
