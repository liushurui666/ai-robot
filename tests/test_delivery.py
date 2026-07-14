import base64
import hashlib
import hmac

import httpx
import pytest

from reminder_mcp.delivery import FeishuDelivery


def test_webhook_signature_matches_feishu_algorithm():
    payload = FeishuDelivery.webhook_payload(
        "摘要", secret="secret", timestamp=1700000000
    )
    key = b"1700000000\nsecret"
    expected = base64.b64encode(
        hmac.new(key, digestmod=hashlib.sha256).digest()
    ).decode()
    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == expected
    assert payload["content"]["text"] == "摘要"


@pytest.mark.asyncio
async def test_webhook_delivery_uses_env_without_exposing_url(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"code": 0})

    monkeypatch.setenv("TEST_WEBHOOK", "https://example.test/hook")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    delivery = FeishuDelivery(client)
    response = await delivery.send(
        {
            "kind": "feishu_webhook",
            "endpoint_env": "TEST_WEBHOOK",
            "secret_env": None,
        },
        "hello",
    )
    await client.aclose()
    assert response == {"code": 0}
    assert seen["url"] == "https://example.test/hook"
    assert '"hello"' in seen["body"]


@pytest.mark.asyncio
async def test_webhook_delivery_reads_explicit_secret_map(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/mapped-hook"
        return httpx.Response(200, json={"code": 0})

    monkeypatch.delenv("MAPPED_WEBHOOK", raising=False)
    monkeypatch.setenv(
        "FEISHU_TARGET_SECRETS_JSON",
        '{"MAPPED_WEBHOOK":"https://example.test/mapped-hook"}',
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    delivery = FeishuDelivery(client)
    response = await delivery.send(
        {
            "kind": "feishu_webhook",
            "endpoint_env": "MAPPED_WEBHOOK",
            "secret_env": None,
        },
        "hello",
    )
    await client.aclose()
    assert response == {"code": 0}


@pytest.mark.asyncio
async def test_internal_chat_includes_idempotency_uuid(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    delivery = FeishuDelivery(client)
    await delivery.send(
        {"kind": "feishu_chat", "recipient": "oc_test"},
        "hello",
        idempotency_key="draft_123",
    )
    await client.aclose()
    body = requests[-1].content.decode()
    assert '"uuid":"draft_123"' in body.replace(" ", "")
