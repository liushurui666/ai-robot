import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nanobot.agent.tools.context import RequestContext

from reminder_mcp.patch_nanobot import main as patch_nanobot

patch_nanobot()

from nanobot.agent.tools.cron import CronTool


class CapturingCron:
    def __init__(self):
        self.job = None

    def add_job(self, **kwargs):
        self.job = kwargs
        return type(
            "Job",
            (),
            {"name": kwargs["name"], "id": "test-job", "enabled": True},
        )()

    def remove_job(self, job_id):
        return "removed"


@pytest.mark.asyncio
async def test_naive_reminder_time_uses_configured_shanghai_timezone():
    config = json.loads(
        (Path(__file__).parents[1] / "config" / "config.example.json").read_text()
    )
    timezone_name = config["agents"]["defaults"]["timezone"]
    assert config["channels"]["feishu"]["streaming"] is False
    assert config["channels"]["feishu"]["processingCard"] is True
    cron = CapturingCron()
    tool = CronTool(cron, default_timezone=timezone_name)
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_test",
            session_key="feishu:ou_test",
        )
    )

    result = await tool.execute(
        action="add",
        name="喝水提醒",
        message="提醒用户喝水",
        at="2026-07-14T12:17:00",
    )

    expected = datetime(
        2026, 7, 14, 12, 17, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert result == "Created job '喝水提醒' (id: test-job)"
    assert cron.job["schedule"].at_ms == int(expected.timestamp() * 1000)
    assert cron.job["origin_channel"] == "feishu"
    assert cron.job["origin_chat_id"] == "ou_test"


@pytest.mark.asyncio
async def test_bounded_recurrence_stores_exclusive_until_in_shanghai_timezone():
    cron = CapturingCron()
    tool = CronTool(cron, default_timezone="Asia/Shanghai")
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_test",
            session_key="feishu:ou_test",
        )
    )

    result = await tool.execute(
        action="add",
        name="bounded-message",
        message="Send a Feishu message to 艾伦: test",
        every_seconds=60,
        until="2099-07-14T16:00:00",
    )

    expected = datetime(
        2099, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert "ending before" in result
    assert cron.job["schedule"].every_ms == 60_000
    assert cron.job["origin_metadata"]["_cron_until_ms"] == int(
        expected.timestamp() * 1000
    )


@pytest.mark.asyncio
async def test_until_is_rejected_for_one_shot_schedule():
    cron = CapturingCron()
    tool = CronTool(cron, default_timezone="Asia/Shanghai")
    tool.set_context(
        RequestContext(
            channel="feishu",
            chat_id="ou_test",
            session_key="feishu:ou_test",
        )
    )

    result = await tool.execute(
        action="add",
        message="test",
        at="2099-07-14T15:30:00",
        until="2099-07-14T16:00:00",
    )

    assert result == "Error: until can only be used with every_seconds or cron_expr"
    assert cron.job is None
