import pytest

from reminder_mcp import server
from reminder_mcp.delivery import TransientDeliveryError
from reminder_mcp.storage import ReminderStore


@pytest.mark.asyncio
async def test_confirm_digest_is_idempotent_after_send(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(
        alias="测试群", kind="feishu_webhook", endpoint_env="TEST_WEBHOOK"
    )
    database.add_record(
        topic="项目进展",
        content="完成登录功能",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="项目进展",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-17T23:59:59+08:00",
        target_alias="测试群",
        summary_text="完成登录功能",
        requested_by="ou_owner",
    )

    calls = []

    async def fake_send(self, target, text, *, dry_run=False, idempotency_key=None):
        calls.append((target["alias"], text))
        assert idempotency_key == draft["id"]
        return {"code": 0}

    monkeypatch.setattr(server.FeishuDelivery, "send", fake_send)
    first = await server.confirm_digest(
        draft["id"], draft["confirmation_token"], "ou_owner"
    )
    second = await server.confirm_digest(
        draft["id"], draft["confirmation_token"], "ou_owner"
    )

    assert first["sent"] is True
    assert second["sent"] is False
    assert second["idempotent"] is True
    assert calls == [("测试群", "完成登录功能")]


@pytest.mark.asyncio
async def test_dry_run_does_not_change_draft_status(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    database.add_record(
        topic="日报",
        content="完成测试",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="日报",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-12T23:59:59+08:00",
        target_alias="项目群",
        summary_text="完成测试",
        requested_by="ou_owner",
    )
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    result = await server.confirm_digest(
        draft["id"], draft["confirmation_token"], "ou_owner", dry_run=True
    )
    assert result["dry_run"] is True
    assert result["draft"]["status"] == "pending"
    assert ReminderStore(db_path).get_draft(draft["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_only_requester_can_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    database.add_record(
        topic="日报",
        content="完成测试",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="日报",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-12T23:59:59+08:00",
        target_alias="项目群",
        summary_text="完成测试",
        requested_by="ou_owner",
    )

    with pytest.raises(PermissionError, match="requested"):
        await server.confirm_digest(
            draft["id"], draft["confirmation_token"], "ou_someone_else", dry_run=True
        )
    assert ReminderStore(db_path).get_draft(draft["id"])["status"] == "pending"


def test_only_requester_can_cancel(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    database.add_record(
        topic="日报",
        content="完成测试",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="日报",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-12T23:59:59+08:00",
        target_alias="项目群",
        summary_text="完成测试",
        requested_by="ou_owner",
    )

    with pytest.raises(PermissionError, match="requested"):
        server.cancel_digest(draft["id"], "ou_other")
    assert ReminderStore(db_path).get_draft(draft["id"])["status"] == "pending"
    result = server.cancel_digest(draft["id"], "ou_owner")
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_confirmation_token_is_required(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    database.add_record(
        topic="日报",
        content="完成测试",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="日报",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-12T23:59:59+08:00",
        target_alias="项目群",
        summary_text="完成测试",
        requested_by="ou_owner",
    )

    with pytest.raises(PermissionError, match="token"):
        await server.confirm_digest(
            draft["id"], "wrong-token", "ou_owner", dry_run=True
        )


@pytest.mark.asyncio
async def test_transient_delivery_is_retried(tmp_path, monkeypatch):
    db_path = tmp_path / "reminder.db"
    monkeypatch.setenv("REMINDER_DB_PATH", str(db_path))
    database = ReminderStore(db_path)
    database.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    database.add_record(
        topic="日报",
        content="完成测试",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = database.create_draft(
        topic="日报",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-12T23:59:59+08:00",
        target_alias="项目群",
        summary_text="完成测试",
        requested_by="ou_owner",
    )
    attempts = []

    async def flaky_send(self, target, text, *, dry_run=False, idempotency_key=None):
        attempts.append(idempotency_key)
        if len(attempts) < 3:
            raise TransientDeliveryError("temporary")
        return {"code": 0}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(server.FeishuDelivery, "send", flaky_send)
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)
    result = await server.confirm_digest(
        draft["id"], draft["confirmation_token"], "ou_owner"
    )
    assert result["sent"] is True
    assert attempts == [draft["id"], draft["id"], draft["id"]]
