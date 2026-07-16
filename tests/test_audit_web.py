import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from reminder_mcp.audit_web import create_app


async def make_client(monkeypatch, tmp_path: Path) -> TestClient:
    state = tmp_path / "nanobot"
    sessions = state / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    media = state / "media" / "feishu"
    media.mkdir(parents=True)
    image = media / "preview.png"
    image.write_bytes(b"png")
    (sessions / "feishu_ou_test.jsonl").write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {"_type": "metadata", "key": "feishu:ou_test", "metadata": {}},
                {
                    "role": "user",
                    "content": "<img src=x onerror=alert(1)>",
                    "media": [str(image)],
                    "timestamp": "2026-07-16T01:00:00",
                },
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("AUDIT_STATE_PATH", str(state))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("AUDIT_ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("AUDIT_RESOLVE_FEISHU_NAMES", "false")
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_all_private_apis_require_login(monkeypatch, tmp_path):
    client = await make_client(monkeypatch, tmp_path)
    try:
        for path in ("/api/overview", "/api/conversations", "/api/uploads", "/api/events"):
            response = await client.get(path)
            assert response.status == 401
        assert (await client.get("/health")).status == 200
        assert (await client.get("/")).status == 200
    finally:
        await client.close()


async def test_login_cookie_allows_read_only_audit_queries(monkeypatch, tmp_path):
    client = await make_client(monkeypatch, tmp_path)
    try:
        bad = await client.post("/api/login", json={"password": "wrong"})
        assert bad.status == 401
        good = await client.post("/api/login", json={"password": "a" * 32})
        assert good.status == 200
        assert "HttpOnly" in good.headers["Set-Cookie"]
        assert "SameSite=Strict" in good.headers["Set-Cookie"]

        overview = await client.get("/api/overview")
        assert overview.status == 200
        assert (await overview.json())["conversations"] == 1

        conversations = await (await client.get("/api/conversations")).json()
        session_id = conversations["conversations"][0]["session_id"]
        detail_response = await client.get(f"/api/conversations/{session_id}")
        assert detail_response.status == 200
        detail = await detail_response.json()
        assert detail["events"][0]["content"] == "<img src=x onerror=alert(1)>"
        assert detail["uploads"][0]["event_id"] == detail["events"][0]["event_id"]
        assert detail["uploads"][0]["content_url"].endswith("/content")
        assert "default-src 'self'" in detail_response.headers["Content-Security-Policy"]
    finally:
        await client.close()


def test_frontend_never_injects_chat_content_as_html():
    app_js = (
        Path(__file__).parents[1]
        / "src"
        / "reminder_mcp"
        / "audit_static"
        / "app.js"
    ).read_text()
    assert ".innerHTML" not in app_js
    assert ".outerHTML" not in app_js
    assert "renderMessageAttachments" in app_js
