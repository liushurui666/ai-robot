import json

import pytest
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)

from reminder_mcp.nanobot_tools import (
    ConfirmDigestTool,
    CreateDigestDraftTool,
    RecordMessageTool,
)
from reminder_mcp.storage import ReminderStore


@pytest.mark.asyncio
async def test_native_tools_bind_current_request_identity(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="测试群", kind="feishu_chat", recipient="oc_target")

    owner_context = RequestContext(
        channel="feishu",
        chat_id="oc_source",
        message_id="om_source",
        metadata={"sender_id": "ou_owner"},
    )
    owner_token = bind_request_context(owner_context)
    try:
        record_result = json.loads(
            await RecordMessageTool().execute(
                topic="日报",
                content="完成身份绑定",
                occurred_at="2026-06-12T09:00:00+08:00",
            )
        )
        record = record_result["record"]
        assert record["author_id"] == "ou_owner"
        assert record["source_chat_id"] == "oc_source"
        assert record["source_message_id"] == "om_source"

        draft_result = json.loads(
            await CreateDigestDraftTool().execute(
                topic="日报",
                range_start="2026-06-12T00:00:00+08:00",
                range_end="2026-06-12T23:59:59+08:00",
                target_alias="测试群",
                summary_text="完成身份绑定",
            )
        )
    finally:
        reset_request_context(owner_token)

    draft = draft_result["draft"]
    assert draft["requested_by"] == "ou_owner"
    other_context = RequestContext(
        channel="feishu",
        chat_id="oc_source",
        message_id="om_other",
        metadata={"sender_id": "ou_other"},
    )
    other_token = bind_request_context(other_context)
    try:
        with pytest.raises(PermissionError, match="requested"):
            await ConfirmDigestTool().execute(
                draft_id=draft["id"],
                confirmation_token=draft["confirmation_token"],
                dry_run=True,
            )
    finally:
        reset_request_context(other_token)

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    owner_token = bind_request_context(owner_context)
    try:
        confirmed = json.loads(
            await ConfirmDigestTool().execute(
                draft_id=draft["id"],
                confirmation_token=draft["confirmation_token"],
                dry_run=True,
            )
        )
    finally:
        reset_request_context(owner_token)
    assert confirmed["dry_run"] is True


@pytest.mark.asyncio
async def test_native_mutation_rejects_missing_request_context(tmp_path, monkeypatch):
    monkeypatch.setenv("REMINDER_DB_PATH", str(tmp_path / "reminder.db"))
    with pytest.raises(PermissionError, match="context"):
        await RecordMessageTool().execute(
            topic="日报",
            content="不应写入",
            occurred_at="2026-06-12T09:00:00+08:00",
        )
