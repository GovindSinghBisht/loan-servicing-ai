"""Start every mock system on its own port in one process.

    uv run python -m mock_systems.run            # all systems
    uv run python -m mock_systems.run loans crm  # a subset

Each system is an independent app with its own data and could equally run as its own container.
"""
from __future__ import annotations

import asyncio
import importlib
import sys

import uvicorn

from .common import PORTS


async def main(names: list[str]) -> None:
    servers = []
    for name in names:
        app = importlib.import_module(f"mock_systems.{name}").app
        servers.append(uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORTS[name], log_level="warning")))
        print(f"  {name:<12} http://127.0.0.1:{PORTS[name]}/docs")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(PORTS)))
