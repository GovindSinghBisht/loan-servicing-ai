"""Run the web API with the offline keyword-routed provider (no LLM key needed). For UI development only.

    uv run python -m mock_systems.run      # terminal 1
    uv run python -m tests.offline_server  # terminal 2, API on :8000
"""
import uvicorn

from engine import server
from tests.offline_llm import OfflineProvider

server.provider_factory = OfflineProvider

if __name__ == "__main__":
    uvicorn.run(server.app, host="127.0.0.1", port=8000, log_level="warning")
