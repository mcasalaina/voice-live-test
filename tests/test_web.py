import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "web"))

from app.main import health


def test_health() -> None:
    assert asyncio.run(health()) == {"status": "ok"}


def test_agent_endpoint_is_not_committed() -> None:
    assert "FOUNDRY_AGENT_WS_ENDPOINT" not in os.environ or os.environ[
        "FOUNDRY_AGENT_WS_ENDPOINT"
    ].startswith(("ws://", "wss://"))
