from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

Notify = Callable[[dict[str, object]], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]

TOOL_DURATION_SECONDS = 15


async def wait_exactly_15_seconds(
    notify: Notify,
    sleep: Sleep = asyncio.sleep,
    duration: int = TOOL_DURATION_SECONDS,
) -> str:
    """Wait for the configured duration and report progress once per second."""
    started = monotonic()
    await notify({"type": "framework_tool_started", "duration_seconds": duration})
    for second in range(1, duration + 1):
        await sleep(1)
        await notify(
            {
                "type": "tool_waiting",
                "elapsed_seconds": second,
                "remaining_seconds": duration - second,
            }
        )
    elapsed = monotonic() - started
    return f"Slow tool completed after {elapsed:.1f} seconds."


async def run_agent_framework_task(
    endpoint: str,
    model: str,
    credential: object,
    notify: Notify,
) -> str:
    """Run the deterministic slow tool through a Microsoft Agent Framework agent."""
    from agent_framework import Agent, tool
    from agent_framework.openai import OpenAIChatCompletionClient

    @tool(approval_mode="never_require")
    async def slow_operation() -> str:
        """Wait exactly 15 seconds before returning a small test result."""
        return await wait_exactly_15_seconds(notify)

    agent = Agent(
        client=OpenAIChatCompletionClient(
            azure_endpoint=endpoint,
            model=model,
            credential=credential,
        ),
        name="slow-tool-agent",
        instructions=(
            "You run one deterministic latency test. For every request, call "
            "slow_operation exactly once, then return its result without extra work."
        ),
        tools=[slow_operation],
    )
    response = await agent.run(
        "Run slow_operation now. You must call the tool exactly once."
    )
    return str(response)
