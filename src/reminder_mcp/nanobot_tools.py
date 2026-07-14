from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import current_request_context

from . import server
from .contacts import FeishuDirectory


def _request_identity() -> tuple[str, str, str]:
    context = current_request_context()
    if context is None:
        raise PermissionError("current nanobot request context is unavailable")
    sender_id = str(
        getattr(context, "sender_id", None)
        or context.metadata.get("sender_id")
        or f"{context.channel}:{context.chat_id}"
    ).strip()
    if not sender_id:
        raise PermissionError("current request has no sender identity")
    return sender_id, str(context.chat_id or ""), str(context.message_id or "")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


STRING = {"type": "string"}


class SendFeishuUserMessageTool(Tool):
    def __init__(self):
        self.directory = FeishuDirectory()

    @property
    def name(self) -> str:
        return "send_feishu_user_message"

    @property
    def description(self) -> str:
        return (
            "Send a direct Feishu text message when the current user explicitly asks to "
            "message a named colleague and provides the message content. Resolve the "
            "recipient from the company directory by name, English name, or nickname. "
            "Send immediately on one unique match; only ask for clarification when the "
            "tool returns no_match or ambiguous. Never ask the user for an open_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "recipient": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            ["recipient", "content"],
        )

    async def execute(self, recipient: str, content: str) -> str:
        _request_identity()
        return _json(await self.directory.send_to_user(recipient, content))


class RecordMessageTool(Tool):
    @property
    def name(self) -> str:
        return "record_message"

    @property
    def description(self) -> str:
        return (
            "Persist one explicitly requested message for a future digest. "
            "The current nanobot sender, chat and message ids are bound automatically. "
            "occurred_at must be ISO-8601 with timezone."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {"topic": STRING, "content": STRING, "occurred_at": STRING},
            ["topic", "content", "occurred_at"],
        )

    async def execute(self, topic: str, content: str, occurred_at: str) -> str:
        sender_id, chat_id, message_id = _request_identity()
        return _json(
            server.record_message(
                topic=topic,
                content=content,
                occurred_at=occurred_at,
                author_id=sender_id,
                source_chat_id=chat_id,
                source_message_id=message_id,
            )
        )


class UpdateRecordTool(Tool):
    @property
    def name(self) -> str:
        return "update_record"

    @property
    def description(self) -> str:
        return "Edit or change status of a record owned by the current nanobot sender."

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "record_id": STRING,
                "content": STRING,
                "status": {
                    "type": "string",
                    "enum": ["active", "cancelled", "archived"],
                },
            },
            ["record_id"],
        )

    async def execute(self, record_id: str, content: str = "", status: str = "") -> str:
        sender_id, _, _ = _request_identity()
        return _json(
            server.update_record(
                record_id=record_id,
                requested_by=sender_id,
                content=content,
                status=status,
            )
        )


class ListDigestDraftsTool(Tool):
    @property
    def name(self) -> str:
        return "list_digest_drafts"

    @property
    def description(self) -> str:
        return "List digest drafts owned by the current nanobot sender."

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "status": {
                    "type": "string",
                    "enum": ["pending", "sending", "sent", "cancelled"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            [],
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, status: str = "", limit: int = 20) -> str:
        sender_id, _, _ = _request_identity()
        return _json(
            server.list_digest_drafts(
                requested_by=sender_id, status=status, limit=limit
            )
        )


class CreateDigestDraftTool(Tool):
    @property
    def name(self) -> str:
        return "create_digest_draft"

    @property
    def description(self) -> str:
        return (
            "Create a pending digest draft owned by the current nanobot sender. "
            "This never sends a message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "topic": STRING,
                "range_start": STRING,
                "range_end": STRING,
                "target_alias": STRING,
                "summary_text": STRING,
            },
            ["topic", "range_start", "range_end", "target_alias", "summary_text"],
        )

    async def execute(
        self,
        topic: str,
        range_start: str,
        range_end: str,
        target_alias: str,
        summary_text: str,
    ) -> str:
        sender_id, _, _ = _request_identity()
        return _json(
            server.create_digest_draft(
                topic=topic,
                range_start=range_start,
                range_end=range_end,
                target_alias=target_alias,
                summary_text=summary_text,
                requested_by=sender_id,
            )
        )


class ConfirmDigestTool(Tool):
    @property
    def name(self) -> str:
        return "confirm_digest"

    @property
    def description(self) -> str:
        return (
            "Send a pending digest after explicit confirmation. The current nanobot sender is "
            "bound automatically and must match the draft requester."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "draft_id": STRING,
                "confirmation_token": STRING,
                "dry_run": {"type": "boolean"},
            },
            ["draft_id", "confirmation_token"],
        )

    async def execute(
        self, draft_id: str, confirmation_token: str, dry_run: bool = False
    ) -> str:
        sender_id, _, _ = _request_identity()
        return _json(
            await server.confirm_digest(
                draft_id=draft_id,
                confirmation_token=confirmation_token,
                confirmed_by=sender_id,
                dry_run=dry_run,
            )
        )


class CancelDigestTool(Tool):
    @property
    def name(self) -> str:
        return "cancel_digest"

    @property
    def description(self) -> str:
        return "Cancel a pending digest owned by the current nanobot sender."

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema({"draft_id": STRING}, ["draft_id"])

    async def execute(self, draft_id: str) -> str:
        sender_id, _, _ = _request_identity()
        return _json(server.cancel_digest(draft_id, sender_id))
