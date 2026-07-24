import json

import pytest
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)

from reminder_mcp.conversation_history import ConversationHistoryReader
from reminder_mcp.nanobot_tools import ListMyRecentConversationTool


class FakeSessions:
    def __init__(self, payload):
        self.payload = payload
        self.requested_keys = []

    def read_session_file(self, key):
        self.requested_keys.append(key)
        return self.payload


def test_lists_only_current_range_and_excludes_cron_and_current_prompt(tmp_path):
    session_key = "feishu:ou_me"
    sessions = FakeSessions(
        {
            "key": session_key,
            "created_at": "2026-07-20T00:00:00",
            "messages": [
                {
                    "role": "user",
                    "content": "上周的事情",
                    "timestamp": "2026-07-19T01:00:00",
                },
                {
                    "role": "user",
                    "content": "推进数据产品规划",
                    "timestamp": "2026-07-21T01:00:00",
                },
                {
                    "role": "assistant",
                    "content": "已安排数据产品规划会议",
                    "timestamp": "2026-07-21T01:01:00",
                },
                {
                    "role": "user",
                    "content": "Scheduled cron job triggered: 喝水",
                    "timestamp": "2026-07-22T04:00:00",
                    "_cron_turn": True,
                },
                {
                    "role": "assistant",
                    "content": "提醒你喝水",
                    "timestamp": "2026-07-22T04:00:01",
                },
                {
                    "role": "user",
                    "content": "帮我把本周事情提炼出来",
                    "timestamp": "2026-07-24T06:10:00",
                },
            ],
        }
    )
    history_path = tmp_path / "memory" / "history.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-20 02:00",
                        "session_key": session_key,
                        "content": "- 已完成旧会话中的方案评审",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-20 03:00",
                        "session_key": "feishu:ou_other",
                        "content": "- 其他人的私聊内容",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-20 04:00",
                        "session_key": session_key,
                        "content": "(skip)",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = ConversationHistoryReader(sessions, tmp_path).list_range(
        session_key=session_key,
        range_start="2026-07-20T00:00:00+08:00",
        range_end="2026-07-24T14:10:00+08:00",
    )

    assert sessions.requested_keys == [session_key]
    assert result["count"] == 3
    assert result["exact_count"] == 2
    assert result["summary_count"] == 1
    contents = [item["content"] for item in result["entries"]]
    assert contents == [
        "- 已完成旧会话中的方案评审",
        "推进数据产品规划",
        "已安排数据产品规划会议",
    ]
    assert all("喝水" not in content for content in contents)
    assert all("提炼" not in content for content in contents)
    assert all("其他人" not in content for content in contents)


def test_requires_explicit_timezone(tmp_path):
    sessions = FakeSessions(None)
    reader = ConversationHistoryReader(sessions, tmp_path)

    with pytest.raises(ValueError, match="timezone"):
        reader.list_range(
            session_key="feishu:ou_me",
            range_start="2026-07-20T00:00:00",
            range_end="2026-07-24T14:10:00+08:00",
        )


@pytest.mark.asyncio
async def test_native_conversation_tool_binds_private_session(tmp_path):
    sessions = FakeSessions(
        {
            "key": "feishu:ou_me",
            "created_at": "2026-07-20T00:00:00",
            "messages": [
                {
                    "role": "user",
                    "content": "完成项目复盘",
                    "timestamp": "2026-07-23T01:00:00",
                },
                {
                    "role": "user",
                    "content": "总结本周",
                    "timestamp": "2026-07-24T06:10:00",
                },
            ],
        }
    )
    tool = ListMyRecentConversationTool(sessions, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="ou_me",
        message_id="om_review",
        session_key="feishu:ou_me",
        metadata={"sender_id": "ou_me", "chat_type": "p2p"},
    )
    token = bind_request_context(context)
    try:
        result = json.loads(
            await tool.execute(
                range_start="2026-07-20T00:00:00+08:00",
                range_end="2026-07-24T14:10:00+08:00",
            )
        )
    finally:
        reset_request_context(token)

    assert result["count"] == 1
    assert result["entries"][0]["content"] == "完成项目复盘"
    assert sessions.requested_keys == ["feishu:ou_me"]


@pytest.mark.asyncio
async def test_native_conversation_tool_rejects_group_chat(tmp_path):
    sessions = FakeSessions(None)
    tool = ListMyRecentConversationTool(sessions, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="oc_group",
        message_id="om_review",
        session_key="feishu:oc_group",
        metadata={"sender_id": "ou_me", "chat_type": "group"},
    )
    token = bind_request_context(context)
    try:
        with pytest.raises(PermissionError, match="private"):
            await tool.execute(
                range_start="2026-07-20T00:00:00+08:00",
                range_end="2026-07-24T14:10:00+08:00",
            )
    finally:
        reset_request_context(token)

    assert sessions.requested_keys == []
