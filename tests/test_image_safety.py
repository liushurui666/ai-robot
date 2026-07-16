from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from loguru import logger

from reminder_mcp.patch_nanobot import main as patch_nanobot

patch_nanobot()

from nanobot.agent.loop import AgentLoop
from nanobot.channels import feishu as feishu_module
from nanobot.channels.feishu import FeishuChannel


def test_active_model_is_vision_capable_and_deterministic():
    root = Path(__file__).parents[1]
    config = json.loads((root / "config" / "config.example.json").read_text())
    primary = config["modelPresets"]["primary"]

    assert primary["model"] == "qwen3.6-plus"
    assert primary["temperature"] == 0.0
    assert "never invent a visual description" in (
        root / "config" / "workspace" / "skills" / "image-analysis" / "SKILL.md"
    ).read_text()


def test_nanobot_image_turns_force_zero_temperature():
    source = inspect.getsource(AgentLoop._run_agent_loop)

    assert 'block.get("type") == "image_url"' in source
    assert "temperature=(" in source


@pytest.mark.asyncio
async def test_feishu_downloads_with_same_filename_do_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(feishu_module, "get_media_dir", lambda channel: tmp_path)
    channel = object.__new__(FeishuChannel)
    channel.logger = logger
    payloads = iter((b"first-image", b"second-image"))
    channel._download_image_sync = lambda message_id, image_key: (
        next(payloads),
        "shared-name.jpg",
    )

    first, _ = await channel._download_and_save_media(
        "image", {"image_key": "img_same_key"}, "om_first_message"
    )
    second, _ = await channel._download_and_save_media(
        "image", {"image_key": "img_same_key"}, "om_second_message"
    )

    assert first != second
    assert Path(first).read_bytes() == b"first-image"
    assert Path(second).read_bytes() == b"second-image"
