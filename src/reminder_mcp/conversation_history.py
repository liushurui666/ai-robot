from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ConversationHistoryReader:
    """Read a bounded time range from one already-authorized nanobot session."""

    def __init__(self, sessions: Any, workspace: str | Path):
        self.sessions = sessions
        self.workspace = Path(workspace).expanduser()
        self.history_path = self.workspace / "memory" / "history.jsonl"

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip().replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        # nanobot 0.2.2 persists naive datetime.now() values in the
        # container's UTC timezone.
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    @classmethod
    def _required_aware_timestamp(cls, value: str, name: str) -> datetime:
        raw = value.strip().replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be an ISO-8601 timestamp with timezone")
        return parsed

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except (FileNotFoundError, OSError):
            pass
        return rows

    @staticmethod
    def _content(row: dict[str, Any]) -> str:
        content = row.get("content")
        if isinstance(content, str):
            return content.strip()
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False)

    def list_range(
        self,
        *,
        session_key: str,
        range_start: str,
        range_end: str,
        max_items: int = 200,
        max_chars: int = 60_000,
    ) -> dict[str, Any]:
        start = self._required_aware_timestamp(range_start, "range_start")
        end = self._required_aware_timestamp(range_end, "range_end")
        if end < start:
            raise ValueError("range_end must not be before range_start")
        if max_items < 1 or max_items > 500:
            raise ValueError("max_items must be between 1 and 500")

        session = self.sessions.read_session_file(session_key)
        if session is None:
            session_messages: list[dict[str, Any]] = []
            session_created_at = None
        else:
            if str(session.get("key") or "") != session_key:
                raise PermissionError("persisted session identity does not match requester")
            raw_messages = session.get("messages")
            session_messages = raw_messages if isinstance(raw_messages, list) else []
            session_created_at = session.get("created_at")

        # The current instruction is persisted before tool execution in
        # nanobot 0.2.2, but it is not source material for its own report.
        current_user_index = next(
            (
                index
                for index in range(len(session_messages) - 1, -1, -1)
                if session_messages[index].get("role") == "user"
                and session_messages[index].get("_cron_turn") is not True
            ),
            None,
        )

        entries: list[dict[str, Any]] = []
        active_turn_is_cron = False
        for index, row in enumerate(session_messages):
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "")
            if role == "user":
                active_turn_is_cron = row.get("_cron_turn") is True
            if role not in {"user", "assistant"} or active_turn_is_cron:
                continue
            if role == "user" and index == current_user_index:
                continue
            content = self._content(row)
            timestamp = self._parse_timestamp(row.get("timestamp"))
            if (
                not content
                or timestamp is None
                or timestamp < start
                or timestamp > end
            ):
                continue
            entries.append(
                {
                    "timestamp": timestamp.astimezone(start.tzinfo).isoformat(
                        timespec="seconds"
                    ),
                    "role": role,
                    "content": content,
                    "source": "exact_conversation",
                }
            )

        summaries: list[dict[str, Any]] = []
        for row in self._read_jsonl(self.history_path):
            if row.get("session_key") != session_key:
                continue
            content = self._content(row)
            timestamp = self._parse_timestamp(row.get("timestamp"))
            if (
                not content
                or content == "(skip)"
                or timestamp is None
                or timestamp < start
                or timestamp > end
            ):
                continue
            summaries.append(
                {
                    "timestamp": timestamp.astimezone(start.tzinfo).isoformat(
                        timespec="seconds"
                    ),
                    "role": "summary",
                    "content": content,
                    "source": "compacted_summary",
                }
            )

        combined = sorted(
            [*summaries, *entries],
            key=lambda item: (item["timestamp"], item["source"], item["role"]),
        )
        selected: list[dict[str, Any]] = []
        used_chars = 0
        truncated = False
        for item in combined:
            content = item["content"]
            if len(content) > 6_000:
                content = content[:5_980] + "\n…单条内容已截断"
                item = {**item, "content": content}
                truncated = True
            item_chars = len(content)
            if len(selected) >= max_items or used_chars + item_chars > max_chars:
                truncated = True
                break
            selected.append(item)
            used_chars += item_chars

        return {
            "range_start": start.isoformat(timespec="seconds"),
            "range_end": end.isoformat(timespec="seconds"),
            "count": len(selected),
            "exact_count": sum(
                1 for item in selected if item["source"] == "exact_conversation"
            ),
            "summary_count": sum(
                1 for item in selected if item["source"] == "compacted_summary"
            ),
            "truncated": truncated,
            "session_created_at": session_created_at,
            "entries": selected,
        }
