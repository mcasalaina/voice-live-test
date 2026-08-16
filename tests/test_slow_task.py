import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "agent"))

from main import (
    ResponseCoordinator,
    ToolRunState,
    tools_disabled_response,
    tools_disabled_session,
)
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


def test_tool_can_only_be_claimed_once() -> None:
    state = ToolRunState()

    assert state.claim("first-call")
    assert not state.claim("second-call")

    state.finish('{"ok": true}')
    assert not state.claim("third-call")
    assert state.output == '{"ok": true}'


def test_follow_up_responses_and_session_disable_tools() -> None:
    assert tools_disabled_response().as_dict() == {"tool_choice": "none"}
    assert tools_disabled_session().as_dict() == {
        "tool_choice": "none",
        "parallel_tool_calls": False,
    }


def test_response_coordinator_waits_for_active_response() -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        async def create(self, **kwargs: object) -> None:
            calls.append(kwargs)

    class FakeConnection:
        response = FakeResponse()

    async def run() -> None:
        responses = ResponseCoordinator()
        connection = FakeConnection()

        await responses.create(connection)
        responses.mark_created("first")
        second = asyncio.create_task(responses.create(connection))
        await asyncio.sleep(0)
        assert len(calls) == 1

        responses.mark_done("first")
        await second
        assert len(calls) == 2

    asyncio.run(run())
