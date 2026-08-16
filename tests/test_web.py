import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "web"))

from app.main import app, health, principal_tenant_id, tenant_is_allowed


def test_health() -> None:
    assert asyncio.run(health()) == {"status": "ok"}


def test_agent_endpoint_is_not_committed() -> None:
    assert "FOUNDRY_AGENT_WS_ENDPOINT" not in os.environ or os.environ[
        "FOUNDRY_AGENT_WS_ENDPOINT"
    ].startswith(("ws://", "wss://"))


def principal_header(tenant_id: str) -> str:
    principal = {
        "claims": [
            {
                "typ": "http://schemas.microsoft.com/identity/claims/tenantid",
                "val": tenant_id,
            }
        ]
    }
    return base64.b64encode(json.dumps(principal).encode()).decode()


def test_principal_tenant_id() -> None:
    assert principal_tenant_id(principal_header("tenant-a")) == "tenant-a"
    assert principal_tenant_id("not-base64") is None


def test_tenant_allowlist(monkeypatch: object) -> None:
    monkeypatch.setenv("ALLOWED_TENANT_IDS", "tenant-a,tenant-b")
    assert tenant_is_allowed(principal_header("tenant-a"))
    assert not tenant_is_allowed(principal_header("tenant-c"))
    assert not tenant_is_allowed(None)


def test_root_redirects_to_sign_in(monkeypatch: object) -> None:
    monkeypatch.setenv("ALLOWED_TENANT_IDS", "tenant-a")
    response = TestClient(app).get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/.auth/login/aad?")
