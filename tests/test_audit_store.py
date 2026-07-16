import json
from pathlib import Path

from reminder_mcp.audit_store import AuditStore


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_imports_messages_summaries_and_uploads_append_only(tmp_path):
    state = tmp_path / "nanobot"
    media = state / "media" / "feishu"
    media.mkdir(parents=True)
    image = media / "proof.png"
    image.write_bytes(b"png")
    session_path = state / "workspace" / "sessions" / "feishu_ou_test.jsonl"
    rows = [
        {
            "_type": "metadata",
            "key": "feishu:ou_test",
            "created_at": "2026-07-16T01:00:00",
            "updated_at": "2026-07-16T01:01:00",
            "metadata": {},
        },
        {
            "role": "user",
            "content": "请看附件\n[image: download failed]",
            "media": [str(image)],
            "timestamp": "2026-07-16T01:00:00",
        },
        {
            "role": "assistant",
            "content": "已收到",
            "timestamp": "2026-07-16T01:01:00",
        },
    ]
    write_jsonl(session_path, rows)
    write_jsonl(
        state / "workspace" / "memory" / "history.jsonl",
        [
            {
                "cursor": 7,
                "timestamp": "2026-07-15 10:18",
                "content": "早期会话摘要",
                "session_key": "feishu:ou_test",
            }
        ],
    )

    store = AuditStore(state, tmp_path / "audit" / "audit.db")
    assert store.refresh() is True
    assert store.refresh() is False

    detail = store.conversation("feishu_ou_test")
    assert detail is not None
    assert [event["content"] for event in detail["events"]] == [
        "请看附件\n[image: download failed]",
        "已收到",
    ]
    assert detail["summaries"][0]["content"] == "早期会话摘要"
    assert {item["status"] for item in detail["uploads"]} == {"failed", "available"}

    recovered = media / "recovered.png"
    recovered.write_bytes(b"recovered-png")
    write_jsonl(
        media / "audit-manifest.jsonl",
        [
            {
                "message_id": "om_test",
                "file_key": "img_test",
                "session_key": "feishu:ou_test",
                "timestamp": "2026-07-16T01:00:00",
                "kind": "image",
                "filename": "recovered.png",
                "path": str(recovered),
            }
        ],
    )
    assert store.refresh() is True
    recovered_detail = store.conversation("feishu_ou_test")
    assert recovered_detail is not None
    assert {item["status"] for item in recovered_detail["uploads"]} == {"available"}
    assert len(recovered_detail["uploads"]) == 2

    # Simulate nanobot idle compaction. Already imported exact rows remain in
    # the independent audit database.
    write_jsonl(session_path, [rows[0], rows[2]])
    assert store.refresh() is False
    compacted = store.conversation("feishu_ou_test")
    assert compacted is not None
    assert compacted["event_total"] == 2


def test_media_path_outside_fixed_root_is_never_downloadable(tmp_path):
    state = tmp_path / "nanobot"
    session_path = state / "workspace" / "sessions" / "feishu_ou_test.jsonl"
    write_jsonl(
        session_path,
        [
            {"_type": "metadata", "key": "feishu:ou_test", "metadata": {}},
            {
                "role": "user",
                "content": "[file: /etc/passwd]",
                "timestamp": "2026-07-16T01:00:00",
            },
        ],
    )
    store = AuditStore(state, tmp_path / "audit.db")
    store.refresh()
    uploads = store.uploads()
    assert len(uploads) == 1
    assert uploads[0]["status"] == "missing"
    assert store.upload_file(uploads[0]["upload_id"]) is None


def test_metadata_key_preserves_group_topic_session(tmp_path):
    state = tmp_path / "nanobot"
    write_jsonl(
        state / "workspace" / "sessions" / "feishu_oc_group_om_topic.jsonl",
        [
            {
                "_type": "metadata",
                "key": "feishu:oc_group:om_topic",
                "metadata": {},
            },
            {"role": "user", "content": "hello", "timestamp": "2026-07-16T01:00:00"},
        ],
    )
    store = AuditStore(state, tmp_path / "audit.db")
    store.refresh()
    rows = store.conversations()
    assert rows[0]["session_key"] == "feishu:oc_group:om_topic"
