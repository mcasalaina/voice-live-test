from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

import websockets
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Voice Live interim response test")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def allowed_tenant_ids() -> set[str]:
    return {
        tenant.strip().lower()
        for tenant in os.getenv("ALLOWED_TENANT_IDS", "").split(",")
        if tenant.strip()
    }


def principal_tenant_id(encoded_principal: str | None) -> str | None:
    if not encoded_principal:
        return None
    try:
        padding = "=" * (-len(encoded_principal) % 4)
        principal = json.loads(
            base64.b64decode(encoded_principal + padding, validate=True)
        )
    except (ValueError, json.JSONDecodeError):
        return None

    for claim in principal.get("claims", []):
        claim_type = str(claim.get("typ", "")).lower()
        if claim_type == "tid" or claim_type.endswith("/tenantid"):
            return str(claim.get("val", "")).lower() or None
    return None


def tenant_is_allowed(encoded_principal: str | None) -> bool:
    allowed = allowed_tenant_ids()
    return not allowed or principal_tenant_id(encoded_principal) in allowed


@app.middleware("http")
async def restrict_tenant(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path not in {"/", "/health"} and not tenant_is_allowed(
        request.headers.get("x-ms-client-principal")
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "This test is limited to approved Microsoft Entra tenants."},
        )
    return await call_next(request)


@app.get("/")
async def index(request: Request) -> Response:
    principal = request.headers.get("x-ms-client-principal")
    if allowed_tenant_ids() and not principal:
        return RedirectResponse(
            url="/.auth/login/aad?post_login_redirect_uri=%2F",
            status_code=302,
        )
    if not tenant_is_allowed(principal):
        return JSONResponse(
            status_code=403,
            content={"detail": "This test is limited to approved Microsoft Entra tenants."},
        )
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
    if not tenant_is_allowed(browser.headers.get("x-ms-client-principal")):
        await browser.close(code=1008, reason="Microsoft Entra tenant is not allowed")
        return

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
