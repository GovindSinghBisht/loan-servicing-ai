"""Run the mock systems in-process (no ports, no fault injection) so tool tests are fast and deterministic."""
import os

os.environ["CHAOS"] = "off"

import httpx
import pytest

from engine.clients import SYSTEMS, Systems
from engine.core import Session
import engine.tools  # noqa: F401  (registers tools)


class RoutingTransport(httpx.AsyncBaseTransport):
    """Send each request to the in-process app that owns its base URL."""

    def __init__(self):
        import importlib
        self.routes = {}
        for name, cfg in SYSTEMS.items():
            app = importlib.import_module(f"mock_systems.{name}").app
            self.routes[httpx.URL(cfg.base_url).port] = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request):
        return await self.routes[request.url.port].handle_async_request(request)


@pytest.fixture
async def systems():
    sy = Systems(transport=RoutingTransport())
    yield sy
    await sy.close()


@pytest.fixture
def session(systems):
    def make(customer_id):
        s = Session(customer_id=customer_id, systems=systems)
        s.begin_turn()
        return s
    return make
