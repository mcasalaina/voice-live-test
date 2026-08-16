from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureStandardVoice,
    FunctionCallOutputItem,
    FunctionTool,
    InputAudioFormat,
    InputTextContentPart,
    InterimResponseTrigger,
    ItemType,
    LlmInterimResponseConfig,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ResponseCreateParams,
    ServerEventType,
    ServerVad,
    StaticInterimResponseConfig,
    ToolChoiceLiteral,
    UserMessageItem,
)
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from slow_task import run_agent_framework_task

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("voice-live-test")

SAMPLE_RATE = 24_000
CHANNELS = 1
VOICE = os.getenv("AZURE_VOICELIVE_VOICE", "en-US-DavisNeural")
MODEL = os.getenv("AZURE_VOICELIVE_MODEL", "gpt-4.1-mini")
OPENAI_MODEL = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "model-router")
INSTRUCTIONS = (
    "You are a concise operations assistant. When the user asks you to check "
    "the status of their simulated operation, use check_operation_status to "
    "retrieve status instead of asking them to trigger anything manually. Call "
    "that tool at most once per session. After it returns, remember and report "
    "the result without calling the tool again. "
    "While the tool is running, keep the conversation responsive and acknowledge "
    "additional user speech briefly. Never claim the check is complete until its "
    "result arrives."
)


@dataclass
class ToolRunState:
    status: Literal["available", "running", "finished"] = "available"
    primary_call_id: str | None = None
    output: str | None = None

    def claim(self, call_id: str) -> bool:
        if self.status != "available":
            return False
        self.status = "running"
        self.primary_call_id = call_id
        return True

    def finish(self, output: str) -> None:
        self.status = "finished"
        self.output = output


@dataclass(frozen=True)
class PendingToolCall:
    call_id: str
    item_id: str


class ResponseCoordinator:
    def __init__(self) -> None:
        self._idle = asyncio.Event()
        self._idle.set()
        self._create_lock = asyncio.Lock()
        self._active_response_id: str | None = None

    async def create(
        self,
        connection: Any,
        *,
        response: ResponseCreateParams | None = None,
        additional_instructions: str | None = None,
    ) -> None:
        async with self._create_lock:
            await self._idle.wait()
            self._idle.clear()
            try:
                await connection.response.create(
                    response=response,
                    additional_instructions=additional_instructions,
                )
            except Exception:
                self._idle.set()
                raise

    def mark_created(self, response_id: str) -> None:
        self._active_response_id = response_id
        self._idle.clear()

    def mark_done(self, response_id: str) -> None:
        if self._active_response_id in {None, response_id}:
            self._active_response_id = None
            self._idle.set()

    def mark_request_failed(self) -> None:
        if self._active_response_id is None:
            self._idle.set()


def account_endpoint() -> str:
    raw = (
        os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        or os.getenv("AZURE_VOICELIVE_ENDPOINT", "").strip()
    )
    if not raw:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT or AZURE_VOICELIVE_ENDPOINT is required"
        )
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise RuntimeError(f"Invalid Foundry endpoint: {raw!r}")
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def openai_endpoint() -> str:
    return os.getenv("AZURE_OPENAI_ENDPOINT", account_endpoint()).rstrip("/") + "/"


def audio_frame(pcm: bytes) -> bytes:
    return struct.pack("<II", SAMPLE_RATE, CHANNELS) + pcm


def interim_config(mode: str) -> Any:
    if mode == "static":
        return StaticInterimResponseConfig(
            triggers=[InterimResponseTrigger.TOOL],
            texts=[
                "The slow tool is still running, but I can keep talking with you.",
                "I am still waiting for the tool. You can continue speaking.",
                "The test is in progress. I am listening.",
            ],
        )
    if mode != "llm":
        raise ValueError(f"Unsupported interim mode: {mode}")
    return LlmInterimResponseConfig(
        triggers=[InterimResponseTrigger.TOOL],
        model=MODEL,
        max_completion_tokens=40,
        instructions=(
            "Say one brief, natural sentence that the slow tool is still running "
            "and invite the user to keep talking. Do not imply completion."
        ),
    )


def build_session(mode: str) -> RequestSession:
    return RequestSession(
        modalities=[Modality.TEXT, Modality.AUDIO],
        instructions=INSTRUCTIONS,
        voice=AzureStandardVoice(name=VOICE),
        input_audio_format=InputAudioFormat.PCM16,
        output_audio_format=OutputAudioFormat.PCM16,
        input_audio_transcription=AudioInputTranscriptionOptions(model="azure-speech"),
        turn_detection=ServerVad(
            threshold=0.5,
            prefix_padding_ms=300,
            silence_duration_ms=500,
        ),
        input_audio_echo_cancellation=AudioEchoCancellation(),
        input_audio_noise_reduction=AudioNoiseReduction(
            type="azure_deep_noise_suppression"
        ),
        tools=[
            FunctionTool(
                name="check_operation_status",
                description=(
                    "Retrieve fresh status for the user's simulated operation. "
                    "This lookup takes 15 seconds. Use it only when no status "
                    "result has been retrieved yet in this session."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice=ToolChoiceLiteral.AUTO,
        parallel_tool_calls=False,
        interim_response=interim_config(mode),
    )


def tools_disabled_response() -> ResponseCreateParams:
    return ResponseCreateParams(tool_choice=ToolChoiceLiteral.NONE)


def tools_disabled_session() -> RequestSession:
    return RequestSession(
        tool_choice=ToolChoiceLiteral.NONE,
        parallel_tool_calls=False,
    )


async def safe_send_json(websocket: WebSocket, payload: dict[str, object]) -> None:
    if websocket.application_state != WebSocketState.DISCONNECTED:
        await websocket.send_json(payload)


async def read_start_message(websocket: WebSocket) -> str:
    message = await websocket.receive_text()
    payload = json.loads(message)
    if payload.get("type") != "start":
        raise ValueError("The first message must be a start control message")
    mode = str(payload.get("interim_mode", "llm"))
    if mode not in {"llm", "static"}:
        raise ValueError("interim_mode must be llm or static")
    return mode


async def browser_to_voicelive(
    websocket: WebSocket,
    connection: Any,
    state: ToolRunState,
    responses: ResponseCoordinator,
) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data:
            await connection.input_audio_buffer.append(
                audio=base64.b64encode(data).decode("ascii")
            )
            continue
        text = message.get("text")
        if not text:
            continue
        payload = json.loads(text)
        if payload.get("type") == "text" and payload.get("content"):
            await connection.conversation.item.create(
                item=UserMessageItem(
                    content=[InputTextContentPart(text=str(payload["content"]))]
                )
            )
            response = (
                ResponseCreateParams()
                if state.status == "available"
                else tools_disabled_response()
            )
            await responses.create(connection, response=response)


async def execute_tool(
    websocket: WebSocket,
    connection: Any,
    pending: PendingToolCall,
    state: ToolRunState,
    responses: ResponseCoordinator,
) -> None:
    call_id = pending.call_id
    await safe_send_json(
        websocket,
        {"type": "tool_started", "call_id": call_id, "duration_seconds": 15},
    )

    async def notify(event: dict[str, object]) -> None:
        await safe_send_json(websocket, {**event, "call_id": call_id})

    sync_credential = SyncDefaultAzureCredential()
    try:
        result = await run_agent_framework_task(
            endpoint=openai_endpoint(),
            model=OPENAI_MODEL,
            credential=sync_credential,
            notify=notify,
        )
        output = json.dumps({"ok": True, "result": result})
        state.finish(output)
        completion_event: dict[str, object] = {
            "type": "tool_completed",
            "call_id": call_id,
        }
    except Exception as exc:
        logger.exception("Agent Framework tool failed")
        output = json.dumps({"ok": False, "error": str(exc)})
        state.finish(output)
        completion_event = {
            "type": "tool_failed",
            "call_id": call_id,
            "message": str(exc),
        }
    finally:
        sync_credential.close()

    await connection.conversation.item.create(
        previous_item_id=pending.item_id,
        item=FunctionCallOutputItem(call_id=call_id, output=output),
    )
    await responses.create(
        connection,
        response=tools_disabled_response(),
        additional_instructions=(
            "The operation status tool has finished. Report its result to the "
            "user as the final answer for this request. Do not call any tool in "
            "this response."
        ),
    )
    await safe_send_json(websocket, completion_event)


async def answer_duplicate_tool_call(
    connection: Any,
    pending: PendingToolCall,
    state: ToolRunState,
) -> None:
    if state.status == "finished" and state.output:
        output = state.output
    else:
        output = json.dumps(
            {
                "ok": False,
                "status": "already_running",
                "message": "The operation status check is already running.",
            }
        )

    await connection.conversation.item.create(
        previous_item_id=pending.item_id,
        item=FunctionCallOutputItem(call_id=pending.call_id, output=output),
    )


async def voicelive_to_browser(
    websocket: WebSocket,
    connection: Any,
    state: ToolRunState,
    responses: ResponseCoordinator,
) -> None:
    pending_calls: dict[str, PendingToolCall] = {}
    tool_task: asyncio.Task[None] | None = None
    session_announced = False

    async for event in connection:
        event_type = event.type
        if event_type == ServerEventType.SESSION_UPDATED:
            if not session_announced:
                session_announced = True
                await safe_send_json(
                    websocket,
                    {
                        "type": "session_started",
                        "session_id": event.session.id,
                        "voice": VOICE,
                        "model": MODEL,
                    },
                )
        elif event_type == ServerEventType.RESPONSE_CREATED:
            responses.mark_created(event.response.id)
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            await safe_send_json(websocket, {"type": "user_speech_started"})
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            await safe_send_json(websocket, {"type": "user_speech_stopped"})
        elif event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
            pcm = event.delta or b""
            if pcm:
                await websocket.send_bytes(audio_frame(pcm))
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            delta = getattr(event, "delta", "") or ""
            if delta:
                await safe_send_json(
                    websocket,
                    {"type": "bot_text", "delta": delta, "final": False},
                )
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            await safe_send_json(
                websocket,
                {
                    "type": "bot_text",
                    "text": getattr(event, "transcript", "") or "",
                    "final": True,
                },
            )
        elif (
            event_type
            == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        ):
            await safe_send_json(
                websocket,
                {
                    "type": "transcription",
                    "text": getattr(event, "transcript", "") or "",
                    "final": True,
                },
            )
        elif event_type == ServerEventType.CONVERSATION_ITEM_CREATED:
            if event.item.type == ItemType.FUNCTION_CALL:
                pending_calls[event.item.call_id] = PendingToolCall(
                    call_id=event.item.call_id,
                    item_id=event.item.id,
                )
        elif event_type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            pending = pending_calls.pop(event.call_id, None)
            if pending:
                if state.claim(pending.call_id):
                    await connection.session.update(session=tools_disabled_session())
                    tool_task = asyncio.create_task(
                        execute_tool(
                            websocket,
                            connection,
                            pending,
                            state,
                            responses,
                        ),
                        name=f"tool-{pending.call_id}",
                    )
                else:
                    await answer_duplicate_tool_call(connection, pending, state)
        elif event_type == ServerEventType.RESPONSE_DONE:
            responses.mark_done(event.response.id)
            await safe_send_json(websocket, {"type": "response_done"})
        elif event_type == ServerEventType.ERROR:
            responses.mark_request_failed()
            error = getattr(event, "error", None)
            await safe_send_json(
                websocket,
                {
                    "type": "error",
                    "message": getattr(error, "message", str(error)),
                    "code": getattr(error, "code", None),
                },
            )

    if tool_task:
        tool_task.cancel()
        with suppress(asyncio.CancelledError):
            await tool_task


app = InvocationAgentServerHost()


@app.ws_handler
async def handle_ws(websocket: WebSocket) -> None:
    credential = DefaultAzureCredential()
    state = ToolRunState()
    try:
        mode = await read_start_message(websocket)
        async with voicelive_connect(
            endpoint=account_endpoint(),
            credential=credential,
            model=MODEL,
        ) as connection:
            responses = ResponseCoordinator()
            await connection.session.update(session=build_session(mode))
            forward = asyncio.create_task(
                browser_to_voicelive(websocket, connection, state, responses),
                name="browser-to-vl",
            )
            backward = asyncio.create_task(
                voicelive_to_browser(websocket, connection, state, responses),
                name="vl-to-browser",
            )
            done, pending_tasks = await asyncio.wait(
                {forward, backward}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending_tasks:
                task.cancel()
            for task in pending_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                error = task.exception()
                if error and not isinstance(error, WebSocketDisconnect):
                    raise error
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("Voice session failed")
        with suppress(Exception):
            await safe_send_json(
                websocket, {"type": "error", "message": str(exc)}
            )
    finally:
        await credential.close()


if __name__ == "__main__":
    app.run()
