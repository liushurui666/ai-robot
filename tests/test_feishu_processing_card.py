import json

import pytest
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, FeishuConfig


def test_processing_card_config_and_content():
    config = FeishuConfig.model_validate(
        {
            "processingCard": True,
            "processingText": "正在处理",
            "reactEmoji": "",
            "streaming": False,
        }
    )
    assert config.processing_card is True
    assert config.processing_text == "正在处理"
    card = json.loads(
        FeishuChannel._processing_card_content("最终结果", processing=False)
    )
    assert card["header"]["title"]["content"] == "处理完成"
    assert card["elements"][0]["content"] == "最终结果"


@pytest.mark.asyncio
async def test_final_response_updates_the_pending_card(monkeypatch):
    channel = FeishuChannel(
        FeishuConfig.model_validate(
            {"processingCard": True, "streaming": False, "reactEmoji": ""}
        ),
        MessageBus(),
    )
    channel._client = object()
    channel._processing_cards["om_source"] = "om_processing"
    updates = []

    def fake_update(message_id, content):
        updates.append((message_id, content))
        return True

    monkeypatch.setattr(channel, "_update_processing_card_sync", fake_update)
    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_user",
            content="处理结果",
            metadata={"message_id": "om_source"},
        )
    )

    assert updates == [("om_processing", "处理结果")]
    assert "om_source" not in channel._processing_cards
