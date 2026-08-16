import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "agent"))

from slow_task import wait_exactly_15_seconds


def test_slow_tool_reports_each_second() -> None:
    events: list[dict[str, object]] = []

    async def notify(event: dict[str, object]) -> None:
        events.append(event)

    async def no_wait(_: float) -> None:
        return None

    result = asyncio.run(wait_exactly_15_seconds(notify, sleep=no_wait))

    assert result.startswith("Slow tool completed")
    assert events[0] == {"type": "framework_tool_started", "duration_seconds": 15}
    assert len([event for event in events if event["type"] == "tool_waiting"]) == 15
    assert events[-1]["remaining_seconds"] == 0
