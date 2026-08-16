from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

import websockets
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Voice Live interim response test")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def browser_to_foundry(browser: WebSocket, foundry: object) -> None:
    while True:
        message = await browser.receive()
        if message.get("type") == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            await foundry.send(message["bytes"])
        elif message.get("text") is not None:
            await foundry.send(message["text"])


async def foundry_to_browser(browser: WebSocket, foundry: object) -> None:
    async for message in foundry:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(message)


@app.websocket("/ws")
async def websocket_proxy(browser: WebSocket) -> None:
    upstream = os.environ.get("FOUNDRY_AGENT_WS_ENDPOINT", "").strip()
    if not upstream:
        await browser.close(code=1011, reason="FOUNDRY_AGENT_WS_ENDPOINT is not set")
        return

    await browser.accept()
    credential = DefaultAzureCredential()
    try:
        token = await credential.get_token("https://ai.azure.com/.default")
        async with websockets.connect(
            upstream,
            additional_headers={"Authorization": f"Bearer {token.token}"},
            max_size=1024 * 1024,
        ) as foundry:
            upload = asyncio.create_task(
                browser_to_foundry(browser, foundry), name="browser-to-foundry"
            )
            download = asyncio.create_task(
                foundry_to_browser(browser, foundry), name="foundry-to-browser"
            )
            _, pending = await asyncio.wait(
                {upload, download}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
    except WebSocketDisconnect:
        return
    except Exception as exc:
        with suppress(Exception):
            await browser.send_json({"type": "error", "message": str(exc)})
            await browser.close(code=1011)
    finally:
        await credential.close()
