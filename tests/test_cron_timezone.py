import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron import CronTool


class CapturingCron:
    def __init__(self):
        self.job = None

    def add_job(self, **kwargs):
        self.job = kwargs
        return type("Job", (), {"name": kwargs["name"], "id": "test-job"})()


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
