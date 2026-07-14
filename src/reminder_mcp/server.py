from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .delivery import FeishuDelivery, TransientDeliveryError
from .storage import ReminderStore

mcp = FastMCP("feishu-reminder")


def store() -> ReminderStore:
    return ReminderStore(
        os.getenv("REMINDER_DB_PATH", "~/.nanobot/reminder/reminder.db")
    )


def record_message(
    topic: str,
    content: str,
    occurred_at: str,
    author_id: str,
    author_name: str = "",
    source_chat_id: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    """Persist one message for a future digest.

    Only call when the user explicitly asks to record/save/collect content. First read the exact
    request.sender_id with nanobot's my tool and pass it as author_id; never invent an identity.
    occurred_at must include a timezone. source_message_id makes repeated calls idempotent.
    """
    record, created = store().add_record(
        topic=topic,
        content=content,
        occurred_at=occurred_at,
        author_id=author_id,
        author_name=author_name or None,
        source_chat_id=source_chat_id or None,
        source_message_id=source_message_id or None,
    )
    return {"created": created, "record": record}


@mcp.tool()
def list_digest_records(
    topic: str, range_start: str, range_end: str, include_archived: bool = False
) -> dict[str, Any]:
    """List persisted source messages for a digest, ordered by occurrence time.

    range_start and range_end must be ISO-8601 timestamps with timezone. Use this before writing
    a summary; never invent source facts that are absent from these records.
    """
    records = store().list_records(
        topic=topic,
        range_start=range_start,
        range_end=range_end,
        include_archived=include_archived,
    )
    return {"count": len(records), "records": records}


@mcp.tool()
def list_topics() -> dict[str, Any]:
    """List known digest topics and their active record counts/date bounds."""
    topics = store().list_topics()
    return {"count": len(topics), "topics": topics}


def update_record(
    record_id: str,
    requested_by: str,
    content: str = "",
    status: str = "",
) -> dict[str, Any]:
    """Edit, cancel, archive, or restore one stored source message.

    Pass status as active, cancelled, or archived. Only the original recorder may modify a record
    that has an author_id. Never use this to edit a message without an explicit user request.
    """
    record = store().update_record(
        record_id,
        requested_by=requested_by,
        content=content or None,
        status=status or None,
    )
    return {"updated": True, "record": record}


@mcp.tool()
def suggest_digest_range(topic: str, range_end: str) -> dict[str, Any]:
    """Resolve the default range for 'since the last digest' behavior.

    The start is one second after the last successfully sent digest, or the first active source
    record when no digest has been sent. range_end must include a timezone.
    """
    return store().suggested_range(topic=topic, range_end=range_end)


@mcp.tool()
def list_delivery_targets() -> dict[str, Any]:
    """List administrator-approved Feishu destinations available for digest delivery."""
    targets = store().list_targets()
    return {"count": len(targets), "targets": targets}


def list_digest_drafts(
    requested_by: str, status: str = "", limit: int = 20
) -> dict[str, Any]:
    """List this requester's recent digest drafts and delivery states.

    First read request.sender_id with nanobot's my tool and pass the exact value. Never list drafts
    with an invented or omitted requester identity.
    """
    drafts = store().list_drafts(
        requested_by=requested_by,
        status=status or None,
        limit=limit,
    )
    return {"count": len(drafts), "drafts": drafts}


def create_digest_draft(
    topic: str,
    range_start: str,
    range_end: str,
    target_alias: str,
    summary_text: str,
    requested_by: str,
) -> dict[str, Any]:
    """Create a pending digest draft after summarizing records returned by list_digest_records.

    This never sends a message. First read request.sender_id with nanobot's my tool and pass it as
    requested_by. Show the returned draft id and exact summary, then ask for confirmation.
    """
    draft = store().create_draft(
        topic=topic,
        range_start=range_start,
        range_end=range_end,
        target_alias=target_alias or None,
        summary_text=summary_text,
        requested_by=requested_by,
    )
    return {"requires_confirmation": True, "draft": draft}


async def confirm_digest(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send a pending digest after the user explicitly confirms that exact draft.

    Never call merely because the user asked to prepare, preview, or schedule a digest. First read
    request.sender_id with nanobot's my tool and pass it as confirmed_by. Calling again for an
    already-sent draft is idempotent and will not send a duplicate.
    """
    database = store()
    draft, claimed = database.claim_pending_draft(
        draft_id, confirmation_token, confirmed_by
    )
    if draft["status"] == "sent":
        return {"sent": False, "idempotent": True, "draft": draft}
    if not claimed:
        return {"sent": False, "already_processing": True, "draft": draft}
    if draft["status"] != "sending":
        raise ValueError(f"draft cannot be sent from status {draft['status']}")
    target = database.get_target(draft["target_alias"])
    delivery = FeishuDelivery()
    try:
        response: dict[str, Any] | None = None
        for attempt in range(1, 4):
            try:
                response = await delivery.send(
                    target,
                    draft["summary_text"],
                    dry_run=dry_run,
                    idempotency_key=draft_id,
                )
                break
            except TransientDeliveryError as exc:
                database.add_delivery_log(
                    draft_id,
                    False,
                    {"attempt": attempt, "transient": True, "message": str(exc)},
                )
                if attempt == 3:
                    raise
                await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
        assert response is not None
        if dry_run:
            updated = database.update_draft_status(
                draft_id, status="pending", confirmed_by=confirmed_by
            )
            return {
                "sent": False,
                "dry_run": True,
                "response": response,
                "draft": updated,
            }
        database.add_delivery_log(draft_id, True, response)
        updated = database.update_draft_status(
            draft_id,
            status="sent",
            confirmed_by=confirmed_by,
            sent_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        return {"sent": True, "draft": updated, "response": response}
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        database.add_delivery_log(draft_id, False, error)
        database.update_draft_status(draft_id, status="pending", error=str(exc))
        raise
    finally:
        await delivery.close()


def cancel_digest(draft_id: str, cancelled_by: str) -> dict[str, Any]:
    """Cancel the current requester's pending digest so it cannot be delivered.

    First read request.sender_id with nanobot's my tool and pass the exact value.
    """
    database = store()
    draft = database.get_draft(draft_id)
    requester = cancelled_by.strip()
    if not requester:
        raise PermissionError("cancelled_by is required")
    if not draft["requested_by"]:
        raise PermissionError("draft has no requester identity and must be recreated")
    if draft["requested_by"] != requester:
        raise PermissionError("only the user who requested this draft may cancel it")
    if draft["status"] == "sent":
        raise ValueError("a sent digest cannot be cancelled")
    if draft["status"] == "cancelled":
        return {"cancelled": False, "idempotent": True, "draft": draft}
    updated = database.update_draft_status(
        draft_id, status="cancelled", confirmed_by=requester
    )
    return {"cancelled": True, "draft": updated}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
