from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path


FEISHU_PATCHES = (
    (
        "verified sender identity",
        """                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,""",
        """                metadata={
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "chat_type": chat_type,""",
    ),
    (
        "processing card configuration",
        '''    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"''',
        '''    streaming: bool = True
    processing_card: bool = False
    processing_text: str = "⏳ 正在处理…"
    domain: Literal["feishu", "lark"] = "feishu"''',
    ),
    (
        "processing card state",
        """        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id

    # ------------------------------------------------------------------""",
        """        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id
        self._processing_cards: OrderedDict[str, str] = OrderedDict()

    # ------------------------------------------------------------------""",
    ),
    (
        "processing card API helpers",
        """    def _create_streaming_card_sync(
""",
        """    @staticmethod
    def _processing_card_content(content: str, *, processing: bool) -> str:
        text = (content or "").strip()
        encoded = text.encode("utf-8")
        if len(encoded) > 20_000:
            text = encoded[:19_940].decode("utf-8", errors="ignore") + "\\n\\n…内容过长，已截断"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue" if processing else "green",
                "title": {
                    "tag": "plain_text",
                    "content": "正在处理" if processing else "处理完成",
                },
            },
            "elements": [{"tag": "markdown", "content": text}],
        }
        return json.dumps(card, ensure_ascii=False)

    def _send_processing_card_sync(
        self, receive_id_type: str, chat_id: str
    ) -> str | None:
        return self._send_message_sync(
            receive_id_type,
            chat_id,
            "interactive",
            self._processing_card_content(
                self.config.processing_text,
                processing=True,
            ),
        )

    def _update_processing_card_sync(self, message_id: str, content: str) -> bool:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(self._processing_card_content(content, processing=False))
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.patch(request)
            if not response.success():
                self.logger.warning(
                    "Failed to update processing card {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return False
            self.logger.info("Processing card {} updated with final response", message_id)
            return True
        except Exception:
            self.logger.exception("Error updating processing card {}", message_id)
            return False

    def _create_streaming_card_sync(
""",
    ),
    (
        "processing card final update",
        """            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            # Handle tool hint messages.""",
        """            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()
            updated_processing_card = False

            # Progress and tool hints must not consume the pending card. Only the
            # final answer replaces the placeholder, keeping the turn in one card.
            source_message_id = msg.metadata.get("message_id")
            if (
                self.config.processing_card
                and source_message_id
                and msg.content
                and msg.content.strip()
                and not msg.metadata.get("_progress")
                and not msg.metadata.get("_stream_delta")
                and not msg.metadata.get("_stream_end")
            ):
                processing_message_id = self._processing_cards.pop(
                    str(source_message_id), None
                )
                if processing_message_id:
                    updated_processing_card = await loop.run_in_executor(
                        None,
                        self._update_processing_card_sync,
                        processing_message_id,
                        msg.content,
                    )
                    if updated_processing_card and not msg.media:
                        return

            # Handle tool hint messages.""",
    ),
    (
        "avoid duplicate final content",
        """            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)""",
        """            if not updated_processing_card and msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)""",
    ),
    (
        "optional reaction",
        """            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content""",
        """            # Add an optional reaction. Processing-card mode can leave
            # reactEmoji empty and avoid requiring reaction-write permissions.
            if self.config.react_emoji:
                task = asyncio.create_task(
                    self._add_reaction(message_id, self.config.react_emoji)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._on_background_task_done)
                task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content""",
    ),
    (
        "create processing card before dispatch",
        """            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(""",
        """            # Forward to message bus. In non-streaming mode, publish a
            # placeholder first and remember its message id for the final update.
            reply_to = chat_id if chat_type == "group" else sender_id
            if self.config.processing_card and not self.config.streaming:
                loop = asyncio.get_running_loop()
                receive_id_type = "chat_id" if reply_to.startswith("oc_") else "open_id"
                processing_message_id = await loop.run_in_executor(
                    None,
                    self._send_processing_card_sync,
                    receive_id_type,
                    reply_to,
                )
                if processing_message_id:
                    self._processing_cards[message_id] = processing_message_id
                    while len(self._processing_cards) > 500:
                        self._processing_cards.popitem(last=False)

            await self._handle_message(""",
    ),
)

AGENT_LOOP_PATCHES = (
    (
        "error response routing metadata",
        """                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))""",
        """                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                        metadata=dict(msg.metadata or {}),
                    ))""",
    ),
)


def _module_path(module_name: str) -> Path:
    spec = find_spec(module_name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"{module_name} is not installed")
    return Path(spec.origin)


def _apply_patches(path: Path, patches: tuple[tuple[str, str, str], ...]) -> None:
    source = path.read_text(encoding="utf-8")
    changed: list[str] = []
    for name, old, new in patches:
        if new in source:
            continue
        if source.count(old) != 1:
            raise RuntimeError(
                f"nanobot source no longer matches the pinned 0.2.2 {name} patch"
            )
        source = source.replace(old, new)
        changed.append(name)
    if changed:
        path.write_text(source, encoding="utf-8")
        print(f"nanobot patches applied to {path}: {', '.join(changed)}")
    else:
        print(f"nanobot patches already applied: {path}")


def main() -> None:
    _apply_patches(_module_path("nanobot.channels.feishu"), FEISHU_PATCHES)
    _apply_patches(_module_path("nanobot.agent.loop"), AGENT_LOOP_PATCHES)


if __name__ == "__main__":
    main()
