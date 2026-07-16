import json

import httpx
import pytest

from reminder_mcp.contacts import FeishuDirectory, FeishuMessagePermissionError


@pytest.mark.asyncio
async def test_unique_directory_name_sends_direct_message(monkeypatch):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        if path.endswith("/contact/v3/departments/0/children"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [{"department_id": "engineering"}],
                    },
                },
            )
        if path.endswith("/contact/v3/users/find_by_department"):
            department_id = request.url.params["department_id"]
            items = (
                [
                    {
                        "open_id": "ou_kilian",
                        "name": "Kilian",
                        "en_name": "Kilian",
                    }
                ]
                if department_id == "engineering"
                else []
            )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"has_more": False, "items": items}},
            )
        if path.endswith("/im/v1/messages"):
            payload = json.loads(request.content)
            sent.append(payload)
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": "om_sent"}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    directory = FeishuDirectory(client)

    result = await directory.send_to_user("kilian", "你好")
    await client.aclose()

    assert result == {
        "sent": True,
        "recipient": "Kilian",
        "message_id": "om_sent",
    }
    assert sent[0]["receive_id"] == "ou_kilian"
    assert json.loads(sent[0]["content"]) == {"text": "你好"}


def test_ambiguous_name_does_not_select_a_recipient():
    users = [
        {"open_id": "ou_1", "name": "Alex"},
        {"open_id": "ou_2", "name": "Alex"},
        {"open_id": "ou_3", "name": "Alexander"},
    ]

    matches = FeishuDirectory.match_users(users, "alex")

    assert [user["open_id"] for user in matches] == ["ou_1", "ou_2"]


@pytest.mark.asyncio
async def test_reads_and_downloads_post_attachments(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        if path.endswith("/im/v1/messages/om_test"):
            post = {
                "zh_cn": {
                    "content": [
                        [
                            {"tag": "img", "image_key": "img_key"},
                            {"tag": "file", "file_key": "file_key", "file_name": "名单.xlsx"},
                        ]
                    ]
                }
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"body": {"content": json.dumps(post)}}]},
                },
            )
        if "/resources/" in path:
            return httpx.Response(
                200,
                content=b"original-file",
                headers={"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=source.bin"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    directory = FeishuDirectory(client)
    attachments = await directory.message_attachments("om_test")
    content, filename = await directory.download_message_attachment(
        "om_test", "file_key", "file"
    )
    await client.aclose()

    assert {(item["kind"], item["file_key"]) for item in attachments} == {
        ("image", "img_key"),
        ("file", "file_key"),
    }
    assert content == b"original-file"
    assert filename == "source.bin"


@pytest.mark.asyncio
async def test_message_permission_error_is_explicit(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(400, json={"code": 99991672, "msg": "access denied"})

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    directory = FeishuDirectory(client)
    with pytest.raises(FeishuMessagePermissionError):
        await directory.message_attachments("om_test")
    await client.aclose()
