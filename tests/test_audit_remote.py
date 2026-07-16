import json

import httpx

from reminder_mcp.audit_remote import RemoteNanobotReader
from reminder_mcp.contacts import FeishuMessagePermissionError


class DirectoryStub:
    async def list_users(self):
        return [{"open_id": "ou_test", "name": "测试用户"}]


class MediaDirectoryStub(DirectoryStub):
    async def message_attachments(self, message_id):
        return [
            {"kind": "image", "file_key": "img_one", "filename": "名单一.png"},
            {"kind": "file", "file_key": "file_two", "filename": "名单二.xlsx"},
        ]

    async def download_message_attachment(self, message_id, file_key, kind):
        return f"bytes:{file_key}".encode(), None


class MissingPermissionDirectoryStub(DirectoryStub):
    async def message_attachments(self, message_id):
        raise FeishuMessagePermissionError("missing scope")

    async def download_message_attachment(self, message_id, file_key, kind):
        raise AssertionError("download must not run without message permission")


async def test_remote_reader_copies_sessions_without_writing_remote(tmp_path):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        if request.url.path == "/webui/bootstrap":
            assert request.headers["authorization"] == f"Bearer {'s' * 32}"
            return httpx.Response(200, json={"token": "short-token", "expires_in": 300})
        assert request.headers["authorization"] == "Bearer short-token"
        path = request.url.params.get("path")
        if path == "sessions/feishu_ou_test.jsonl":
            content = "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in [
                    {"_type": "metadata", "key": "feishu:ou_test", "metadata": {}},
                    {"role": "user", "content": "hello", "timestamp": "2026-07-16T01:00:00"},
                ]
            ) + "\n"
            return httpx.Response(200, json={"content": content, "truncated": False})
        if path == "memory/history.jsonl":
            return httpx.Response(200, json={"content": "", "truncated": False})
        if path == "cron/jobs.json":
            return httpx.Response(200, json={"content": '{"jobs":[]}', "truncated": False})
        return httpx.Response(404, json={"error": "file not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = RemoteNanobotReader(
        base_url="https://nanobot.example",
        secret="s" * 32,
        target_state_path=tmp_path / "source",
        directory=DirectoryStub(),  # type: ignore[arg-type]
        client=client,
    )
    try:
        assert await reader.sync_once() is True
        session = tmp_path / "source" / "workspace" / "sessions" / "feishu_ou_test.jsonl"
        assert '"content": "hello"' in session.read_text()
        assert reader.status["connected"] is True
        assert all(method == "GET" for method, _path in requested)
    finally:
        await client.aclose()


async def test_truncated_remote_prefix_does_not_replace_complete_cache(tmp_path):
    target = tmp_path / "source"
    session = target / "workspace" / "sessions" / "feishu_ou_test.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("complete-history\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/webui/bootstrap":
            return httpx.Response(200, json={"token": "token", "expires_in": 300})
        path = request.url.params.get("path")
        if path == "sessions/feishu_ou_test.jsonl":
            return httpx.Response(200, json={"content": "partial", "truncated": True})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = RemoteNanobotReader(
        base_url="https://nanobot.example",
        secret="s" * 32,
        target_state_path=target,
        directory=DirectoryStub(),  # type: ignore[arg-type]
        client=client,
    )
    try:
        await reader.sync_once()
        assert session.read_text() == "complete-history\n"
        assert "feishu_ou_test.jsonl" in reader.truncated_files
    finally:
        await client.aclose()


async def test_remote_reader_recovers_feishu_media_into_local_manifest(tmp_path):
    message_id = "om_abc123"
    session_rows = [
        {"_type": "metadata", "key": "feishu:ou_test", "metadata": {}},
        {
            "role": "user",
            "content": "附件\n[image: download failed]\n[file: download failed]",
            "timestamp": "2026-07-16T01:00:00",
        },
    ]
    jobs = {
        "jobs": [
            {
                "createdAtMs": 1784163620000,
                "payload": {
                    "sessionKey": "feishu:ou_test",
                    "originMetadata": {"message_id": message_id},
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/webui/bootstrap":
            return httpx.Response(200, json={"token": "token", "expires_in": 300})
        path = request.url.params.get("path")
        if path == "sessions/feishu_ou_test.jsonl":
            content = "\n".join(json.dumps(row) for row in session_rows) + "\n"
            return httpx.Response(200, json={"content": content, "truncated": False})
        if path == "cron/jobs.json":
            return httpx.Response(200, json={"content": json.dumps(jobs), "truncated": False})
        if path == "memory/history.jsonl":
            return httpx.Response(200, json={"content": "", "truncated": False})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = RemoteNanobotReader(
        base_url="https://nanobot.example",
        secret="s" * 32,
        target_state_path=tmp_path / "source",
        directory=MediaDirectoryStub(),  # type: ignore[arg-type]
        client=client,
    )
    try:
        assert await reader.sync_once() is True
        media = tmp_path / "source" / "media" / "feishu"
        assert len([path for path in media.iterdir() if path.name != "audit-manifest.jsonl"]) == 2
        manifest = [json.loads(line) for line in (media / "audit-manifest.jsonl").read_text().splitlines()]
        assert {row["kind"] for row in manifest} == {"image", "file"}
        assert {row["timestamp"] for row in manifest} == {"2026-07-16T01:00:00"}
        assert reader.status["media_connected"] is True
    finally:
        await client.aclose()


async def test_remote_reader_reports_missing_feishu_message_permission(tmp_path):
    target = tmp_path / "source"
    (target / "workspace" / "cron").mkdir(parents=True)
    (target / "workspace" / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "payload": {
                            "sessionKey": "feishu:ou_test",
                            "originMetadata": {"message_id": "om_abc123"},
                        }
                    }
                ]
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/webui/bootstrap":
            return httpx.Response(200, json={"token": "token", "expires_in": 300})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = RemoteNanobotReader(
        base_url="https://nanobot.example",
        secret="s" * 32,
        target_state_path=target,
        directory=MissingPermissionDirectoryStub(),  # type: ignore[arg-type]
        client=client,
    )
    try:
        await reader.sync_once()
        assert reader.status["connected"] is True
        assert reader.status["media_permission_required"] is True
        assert reader.status["media_last_error"] == "permission_required"
    finally:
        await client.aclose()
