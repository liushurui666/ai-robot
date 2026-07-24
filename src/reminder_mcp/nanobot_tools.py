from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext, current_request_context

from . import server
from .calendar_booking import FeishuCalendarBooker
from .contacts import FeishuDirectory
from .conversation_history import ConversationHistoryReader


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


class BookFeishuMeetingTool(Tool):
    @property
    def name(self) -> str:
        return "book_feishu_meeting"

    @property
    def description(self) -> str:
        return (
            "Book a Feishu calendar meeting only when the current user explicitly asks to "
            "book, reserve, or schedule it. Resolve named colleagues, optionally resolve a "
            "physical meeting room, check relevant availability, create the event, and invite "
            "the requester and colleagues. A room is optional: omit it when the user did not "
            "request one or says no room is needed. Times must be ISO-8601 with timezone. Use "
            "30 minutes when the user gives no duration. Never claim success unless booked=true."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "attendees": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 20,
                },
                "room": {"type": "string", "default": ""},
                "summary": {"type": "string", "minLength": 1, "maxLength": 500},
                "start_time": {"type": "string", "minLength": 1},
                "duration_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "default": 30,
                },
            },
            ["attendees", "summary", "start_time"],
        )

    async def execute(
        self,
        attendees: list[str],
        summary: str,
        start_time: str,
        room: str = "",
        duration_minutes: int = 30,
    ) -> str:
        sender_id, _, message_id = _request_identity()
        booker = FeishuCalendarBooker()
        try:
            return _json(
                await booker.book_meeting(
                    requester_open_id=sender_id,
                    source_message_id=message_id,
                    attendee_names=attendees,
                    room_name=room,
                    summary=summary,
                    start_time=start_time,
                    duration_minutes=duration_minutes,
                )
            )
        finally:
            await booker.close()


class AddRoomToFeishuMeetingTool(Tool):
    @property
    def name(self) -> str:
        return "add_room_to_feishu_meeting"

    @property
    def description(self) -> str:
        return (
            "Add a physical meeting room to a Feishu meeting previously created by "
            "book_feishu_meeting. Use only when the current user explicitly asks to add a room "
            "to that existing meeting. event_id must come from the prior booking tool result; "
            "never ask the user for it and never invent it. The requester must already attend "
            "the meeting. Check room availability and update the original event in place."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "event_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "room": {"type": "string", "minLength": 1},
            },
            ["event_id", "room"],
        )

    async def execute(self, event_id: str, room: str) -> str:
        sender_id, _, _ = _request_identity()
        booker = FeishuCalendarBooker()
        try:
            return _json(
                await booker.add_room_to_meeting(
                    requester_open_id=sender_id,
                    event_id=event_id,
                    room_name=room,
                )
            )
        finally:
            await booker.close()


class ListMyRecentConversationTool(Tool):
    def __init__(self, sessions: Any, workspace: str | Path):
        self.reader = ConversationHistoryReader(sessions, workspace)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ctx.sessions, ctx.workspace)

    @property
    def name(self) -> str:
        return "list_my_recent_conversation"

    @property
    def description(self) -> str:
        return (
            "Read the current Feishu user's own private conversation with this assistant for "
            "a bounded time range, including exact retained turns and same-session compacted "
            "summaries. Use for requests such as summarize my work this week/today/recently. "
            "This is read-only, identity-bound, and unavailable in group chats. It never reads "
            "another user's conversation. Times must be ISO-8601 with timezone."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "range_start": {"type": "string", "minLength": 1},
                "range_end": {"type": "string", "minLength": 1},
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            ["range_start", "range_end"],
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self, range_start: str, range_end: str, max_items: int = 200
    ) -> str:
        context = current_request_context()
        if context is None:
            raise PermissionError("current nanobot request context is unavailable")
        sender_id = str(
            getattr(context, "sender_id", None)
            or context.metadata.get("sender_id")
            or ""
        ).strip()
        chat_id = str(context.chat_id or "").strip()
        session_key = str(context.session_key or "").strip()
        chat_type = str(context.metadata.get("chat_type") or "").strip()
        if (
            context.channel != "feishu"
            or not sender_id
            or not chat_id
            or sender_id != chat_id
            or session_key != f"feishu:{chat_id}"
            or (chat_type and chat_type != "p2p")
        ):
            raise PermissionError(
                "conversation review is available only in the requester's private Feishu chat"
            )
        return _json(
            self.reader.list_range(
                session_key=session_key,
                range_start=range_start,
                range_end=range_end,
                max_items=max_items,
            )
        )


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
