from datetime import datetime, timezone

import pytest

from reminder_mcp.storage import ReminderStore


def test_cross_day_records_create_draft_and_preserve_sources(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    store.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")

    first, created = store.add_record(
        topic="项目进展",
        content="登录功能已经完成",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
        source_message_id="om_12",
    )
    assert created is True
    second, _ = store.add_record(
        topic="项目进展",
        content="支付接口联调存在超时",
        occurred_at="2026-06-15T15:00:00+08:00",
        author_id="ou_owner",
        source_message_id="om_15",
    )

    records = store.list_records(
        topic="项目进展",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-17T23:59:59+08:00",
    )
    assert [record["id"] for record in records] == [first["id"], second["id"]]

    draft = store.create_draft(
        topic="项目进展",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-17T23:59:59+08:00",
        target_alias="项目群",
        summary_text="已完成登录功能；支付接口存在超时风险。",
        requested_by="ou_requester",
    )
    assert draft["status"] == "pending"
    assert set(draft["record_ids"]) == {first["id"], second["id"]}

    # Draft creation must not delete or mutate source records.
    assert (
        len(
            store.list_records(
                topic="项目进展",
                range_start="2026-06-12T00:00:00+08:00",
                range_end="2026-06-17T23:59:59+08:00",
            )
        )
        == 2
    )


def test_source_message_id_is_idempotent(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    kwargs = {
        "topic": "日报",
        "content": "第一条",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "author_id": "ou_owner",
        "source_message_id": "om_same",
    }
    first, first_created = store.add_record(**kwargs)
    second, second_created = store.add_record(**kwargs)
    assert first_created is True
    assert second_created is False
    assert first["id"] == second["id"]


def test_naive_times_are_rejected(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    with pytest.raises(ValueError, match="timezone"):
        store.add_record(
            topic="日报",
            content="内容",
            occurred_at="2026-06-12T09:00:00",
            author_id="ou_owner",
        )


def test_draft_requires_registered_target(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    store.add_record(
        topic="日报",
        content="内容",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    with pytest.raises(ValueError, match="unknown or disabled target"):
        store.create_draft(
            topic="日报",
            range_start="2026-06-12T00:00:00+08:00",
            range_end="2026-06-12T23:59:59+08:00",
            target_alias="未登记群",
            summary_text="摘要",
            requested_by="ou_owner",
        )


def test_suggested_range_starts_after_last_sent_digest(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    store.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    store.add_record(
        topic="项目进展",
        content="第一阶段",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    draft = store.create_draft(
        topic="项目进展",
        range_start="2026-06-12T00:00:00+08:00",
        range_end="2026-06-17T17:00:00+08:00",
        target_alias="项目群",
        summary_text="第一阶段",
        requested_by="ou_owner",
    )
    store.update_draft_status(
        draft["id"],
        status="sent",
        confirmed_by="ou_owner",
        sent_at="2026-06-17T09:00:00+00:00",
    )
    suggested = store.suggested_range(
        topic="项目进展", range_end="2026-06-20T17:00:00+08:00"
    )
    assert suggested["source"] == "after_last_sent_digest"
    assert suggested["range_start"] == "2026-06-17T09:00:01+00:00"
    assert suggested["previous_draft_id"] == draft["id"]


def test_record_owner_can_cancel_but_other_user_cannot(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    record, _ = store.add_record(
        topic="项目进展",
        content="需要修改",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    with pytest.raises(PermissionError):
        store.update_record(record["id"], requested_by="ou_other", status="cancelled")
    updated = store.update_record(
        record["id"], requested_by="ou_owner", content="已修改", status="active"
    )
    assert updated["content"] == "已修改"
    assert updated["status"] == "active"


def test_record_requires_author_identity(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    with pytest.raises(ValueError, match="author_id"):
        store.add_record(
            topic="日报",
            content="内容",
            occurred_at="2026-06-12T09:00:00+08:00",
            author_id=" ",
        )


def test_draft_requires_requester_identity(tmp_path):
    store = ReminderStore(tmp_path / "reminder.db")
    store.add_target(alias="项目群", kind="feishu_chat", recipient="oc_test")
    store.add_record(
        topic="日报",
        content="内容",
        occurred_at="2026-06-12T09:00:00+08:00",
        author_id="ou_owner",
    )
    with pytest.raises(ValueError, match="requested_by"):
        store.create_draft(
            topic="日报",
            range_start="2026-06-12T00:00:00+08:00",
            range_end="2026-06-12T23:59:59+08:00",
            target_alias="项目群",
            summary_text="摘要",
            requested_by=" ",
        )
