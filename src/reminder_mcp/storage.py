from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_timestamp(value: str, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class ReminderStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT,
                    default_target_alias TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_records (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL REFERENCES topics(id),
                    content TEXT NOT NULL,
                    author_id TEXT,
                    author_name TEXT,
                    source_chat_id TEXT,
                    source_message_id TEXT,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(source_message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_records_topic_time
                ON message_records(topic_id, occurred_at);

                CREATE TABLE IF NOT EXISTS delivery_targets (
                    alias TEXT PRIMARY KEY COLLATE NOCASE,
                    kind TEXT NOT NULL CHECK(kind IN ('feishu_webhook', 'feishu_chat')),
                    recipient TEXT,
                    endpoint_env TEXT,
                    secret_env TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS summary_drafts (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL REFERENCES topics(id),
                    range_start TEXT NOT NULL,
                    range_end TEXT NOT NULL,
                    target_alias TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    requested_by TEXT,
                    confirmed_by TEXT,
                    confirmation_token_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS summary_items (
                    draft_id TEXT NOT NULL REFERENCES summary_drafts(id),
                    record_id TEXT NOT NULL REFERENCES message_records(id),
                    PRIMARY KEY(draft_id, record_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_logs (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES summary_drafts(id),
                    attempted_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    response_json TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(summary_drafts)").fetchall()
            }
            if "confirmation_token_hash" not in columns:
                db.execute(
                    "ALTER TABLE summary_drafts ADD COLUMN confirmation_token_hash TEXT"
                )

    def ensure_topic(
        self,
        name: str,
        description: str | None = None,
        default_target_alias: str | None = None,
    ) -> dict[str, Any]:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("topic name cannot be empty")
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM topics WHERE name = ? COLLATE NOCASE", (cleaned,)
            ).fetchone()
            if existing:
                if description is not None or default_target_alias is not None:
                    db.execute(
                        """UPDATE topics SET
                           description = COALESCE(?, description),
                           default_target_alias = COALESCE(?, default_target_alias)
                           WHERE id = ?""",
                        (description, default_target_alias, existing["id"]),
                    )
                    existing = db.execute(
                        "SELECT * FROM topics WHERE id = ?", (existing["id"],)
                    ).fetchone()
                return dict(existing)
            topic_id = new_id("topic")
            db.execute(
                "INSERT INTO topics VALUES (?, ?, ?, ?, ?)",
                (topic_id, cleaned, description, default_target_alias, utc_now()),
            )
            return dict(
                db.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
            )

    def add_record(
        self,
        *,
        topic: str,
        content: str,
        occurred_at: str,
        author_id: str,
        author_name: str | None = None,
        source_chat_id: str | None = None,
        source_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        text = content.strip()
        if not text:
            raise ValueError("content cannot be empty")
        owner = author_id.strip()
        if not owner:
            raise ValueError("author_id is required")
        normalized_time = normalize_timestamp(occurred_at, "occurred_at")
        topic_row = self.ensure_topic(topic)
        with self.connect() as db:
            if source_message_id:
                existing = db.execute(
                    "SELECT * FROM message_records WHERE source_message_id = ?",
                    (source_message_id,),
                ).fetchone()
                if existing:
                    return self._record_dict(existing, topic_row["name"]), False
            record_id = new_id("msg")
            db.execute(
                """INSERT INTO message_records
                   (id, topic_id, content, author_id, author_name, source_chat_id,
                    source_message_id, occurred_at, created_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    topic_row["id"],
                    text,
                    owner,
                    author_name,
                    source_chat_id,
                    source_message_id,
                    normalized_time,
                    utc_now(),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            row = db.execute(
                "SELECT * FROM message_records WHERE id = ?", (record_id,)
            ).fetchone()
            return self._record_dict(row, topic_row["name"]), True

    @staticmethod
    def _record_dict(row: sqlite3.Row, topic_name: str) -> dict[str, Any]:
        result = dict(row)
        result["topic"] = topic_name
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def list_records(
        self,
        *,
        topic: str,
        range_start: str,
        range_end: str,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        start_utc = normalize_timestamp(range_start, "range_start")
        end_utc = normalize_timestamp(range_end, "range_end")
        if start_utc > end_utc:
            raise ValueError("range_start must not be after range_end")
        status_clause = "" if include_archived else "AND r.status = 'active'"
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT r.*, t.name AS topic_name FROM message_records r
                    JOIN topics t ON t.id = r.topic_id
                    WHERE t.name = ? COLLATE NOCASE
                      AND r.occurred_at >= ? AND r.occurred_at <= ? {status_clause}
                    ORDER BY r.occurred_at, r.created_at""",
                (topic.strip(), start_utc, end_utc),
            ).fetchall()
            return [self._record_dict(row, row["topic_name"]) for row in rows]

    def list_topics(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT t.id, t.name, t.description, t.default_target_alias,
                          COUNT(r.id) AS active_record_count,
                          MIN(r.occurred_at) AS first_record_at,
                          MAX(r.occurred_at) AS last_record_at
                   FROM topics t
                   LEFT JOIN message_records r ON r.topic_id=t.id AND r.status='active'
                   GROUP BY t.id ORDER BY t.name COLLATE NOCASE"""
            ).fetchall()
            return [dict(row) for row in rows]

    def update_record(
        self,
        record_id: str,
        *,
        requested_by: str,
        content: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if status not in {None, "active", "cancelled", "archived"}:
            raise ValueError("status must be active, cancelled, or archived")
        if content is not None and not content.strip():
            raise ValueError("content cannot be empty")
        with self.connect() as db:
            row = db.execute(
                """SELECT r.*, t.name AS topic_name FROM message_records r
                   JOIN topics t ON t.id=r.topic_id WHERE r.id=?""",
                (record_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"unknown record: {record_id}")
            if row["author_id"] and row["author_id"] != requested_by:
                raise PermissionError(
                    "only the user who recorded this message may modify it"
                )
            db.execute(
                """UPDATE message_records SET content=COALESCE(?, content),
                   status=COALESCE(?, status) WHERE id=?""",
                (content.strip() if content is not None else None, status, record_id),
            )
            updated = db.execute(
                """SELECT r.*, t.name AS topic_name FROM message_records r
                   JOIN topics t ON t.id=r.topic_id WHERE r.id=?""",
                (record_id,),
            ).fetchone()
            return self._record_dict(updated, updated["topic_name"])

    def suggested_range(self, *, topic: str, range_end: str) -> dict[str, Any]:
        end_utc = normalize_timestamp(range_end, "range_end")
        with self.connect() as db:
            topic_row = db.execute(
                "SELECT id, name FROM topics WHERE name=? COLLATE NOCASE",
                (topic.strip(),),
            ).fetchone()
            if not topic_row:
                raise ValueError(f"unknown topic: {topic}")
            last_sent = db.execute(
                """SELECT range_end, id FROM summary_drafts
                   WHERE topic_id=? AND status='sent' AND range_end < ?
                   ORDER BY range_end DESC LIMIT 1""",
                (topic_row["id"], end_utc),
            ).fetchone()
            if last_sent:
                start = datetime.fromisoformat(last_sent["range_end"]) + timedelta(
                    seconds=1
                )
                source = "after_last_sent_digest"
                previous_draft_id = last_sent["id"]
            else:
                first = db.execute(
                    """SELECT MIN(occurred_at) AS first_at FROM message_records
                       WHERE topic_id=? AND status='active' AND occurred_at <= ?""",
                    (topic_row["id"], end_utc),
                ).fetchone()
                if not first or not first["first_at"]:
                    raise ValueError("no active records exist on or before range_end")
                start = datetime.fromisoformat(first["first_at"])
                source = "first_active_record"
                previous_draft_id = None
            return {
                "topic": topic_row["name"],
                "range_start": start.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "range_end": end_utc,
                "source": source,
                "previous_draft_id": previous_draft_id,
            }

    def add_target(
        self,
        *,
        alias: str,
        kind: str,
        recipient: str | None = None,
        endpoint_env: str | None = None,
        secret_env: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"feishu_webhook", "feishu_chat"}:
            raise ValueError("unsupported target kind")
        if kind == "feishu_webhook" and not endpoint_env:
            raise ValueError("feishu_webhook requires endpoint_env")
        if kind == "feishu_chat" and not recipient:
            raise ValueError("feishu_chat requires recipient chat_id")
        with self.connect() as db:
            db.execute(
                """INSERT INTO delivery_targets
                   (alias, kind, recipient, endpoint_env, secret_env, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(alias) DO UPDATE SET kind=excluded.kind,
                   recipient=excluded.recipient, endpoint_env=excluded.endpoint_env,
                   secret_env=excluded.secret_env, enabled=1""",
                (alias.strip(), kind, recipient, endpoint_env, secret_env, utc_now()),
            )
            return dict(
                db.execute(
                    "SELECT alias, kind, recipient, endpoint_env, secret_env, enabled FROM delivery_targets WHERE alias = ? COLLATE NOCASE",
                    (alias.strip(),),
                ).fetchone()
            )

    def list_targets(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT alias, kind, recipient, enabled FROM delivery_targets WHERE enabled = 1 ORDER BY alias"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_draft(
        self,
        *,
        topic: str,
        range_start: str,
        range_end: str,
        target_alias: str | None,
        summary_text: str,
        requested_by: str,
    ) -> dict[str, Any]:
        if not summary_text.strip():
            raise ValueError("summary_text cannot be empty")
        requester = requested_by.strip()
        if not requester:
            raise ValueError("requested_by is required")
        start_utc = normalize_timestamp(range_start, "range_start")
        end_utc = normalize_timestamp(range_end, "range_end")
        records = self.list_records(
            topic=topic, range_start=start_utc, range_end=end_utc
        )
        if not records:
            raise ValueError("no records found for this topic and time range")
        with self.connect() as db:
            topic_row = db.execute(
                "SELECT * FROM topics WHERE name = ? COLLATE NOCASE", (topic.strip(),)
            ).fetchone()
            resolved_target = target_alias or topic_row["default_target_alias"]
            if not resolved_target:
                raise ValueError(
                    "target_alias is required because the topic has no default target"
                )
            target = db.execute(
                "SELECT alias FROM delivery_targets WHERE alias = ? COLLATE NOCASE AND enabled = 1",
                (resolved_target,),
            ).fetchone()
            if not target:
                raise ValueError(f"unknown or disabled target: {resolved_target}")
            draft_id = new_id("draft")
            confirmation_token = secrets.token_urlsafe(18)
            confirmation_token_hash = hashlib.sha256(
                confirmation_token.encode()
            ).hexdigest()
            db.execute(
                """INSERT INTO summary_drafts
                   (id, topic_id, range_start, range_end, target_alias, summary_text,
                    requested_by, confirmation_token_hash, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    draft_id,
                    topic_row["id"],
                    start_utc,
                    end_utc,
                    target["alias"],
                    summary_text.strip(),
                    requester,
                    confirmation_token_hash,
                    utc_now(),
                ),
            )
            db.executemany(
                "INSERT INTO summary_items(draft_id, record_id) VALUES (?, ?)",
                [(draft_id, record["id"]) for record in records],
            )
            draft = self.get_draft(draft_id, db=db)
            draft["confirmation_token"] = confirmation_token
            return draft

    def list_drafts(
        self,
        *,
        requested_by: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        clauses: list[str] = []
        params: list[Any] = []
        if requested_by:
            clauses.append("d.requested_by=?")
            params.append(requested_by)
        if status:
            clauses.append("d.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT d.id, t.name AS topic, d.range_start, d.range_end,
                           d.target_alias, d.summary_text, d.requested_by, d.confirmed_by,
                           d.status, d.created_at, d.sent_at, d.error,
                           COUNT(si.record_id) AS record_count
                    FROM summary_drafts d
                    JOIN topics t ON t.id=d.topic_id
                    LEFT JOIN summary_items si ON si.draft_id=d.id
                    {where}
                    GROUP BY d.id ORDER BY d.created_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_pending_draft(
        self, draft_id: str, confirmation_token: str, confirmed_by: str
    ) -> tuple[dict[str, Any], bool]:
        """Atomically move a pending draft to sending to prevent duplicate delivery."""
        confirmer = confirmed_by.strip()
        if not confirmer:
            raise PermissionError("confirmed_by is required")
        with self.connect() as db:
            draft = self.get_draft(draft_id, db=db)
            if not draft["requested_by"]:
                raise PermissionError(
                    "draft has no requester identity and must be recreated"
                )
            token_row = db.execute(
                "SELECT confirmation_token_hash FROM summary_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            supplied_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
            expected_hash = token_row["confirmation_token_hash"] if token_row else None
            if not expected_hash or not secrets.compare_digest(
                supplied_hash, expected_hash
            ):
                raise PermissionError("invalid confirmation token")
            if draft["requested_by"] != confirmer:
                raise PermissionError(
                    "only the user who requested this draft may confirm it"
                )
            if draft["status"] != "pending":
                return draft, False
            cursor = db.execute(
                """UPDATE summary_drafts SET status='sending', confirmed_by=?, error=NULL
                   WHERE id=? AND status='pending'""",
                (confirmer, draft_id),
            )
            if cursor.rowcount != 1:
                return self.get_draft(draft_id, db=db), False
            return self.get_draft(draft_id, db=db), True

    def get_draft(
        self, draft_id: str, db: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        def load(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """SELECT d.*, t.name AS topic FROM summary_drafts d
                   JOIN topics t ON t.id = d.topic_id WHERE d.id = ?""",
                (draft_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"unknown draft: {draft_id}")
            result = dict(row)
            result.pop("confirmation_token_hash", None)
            result["record_ids"] = [
                item["record_id"]
                for item in connection.execute(
                    "SELECT record_id FROM summary_items WHERE draft_id = ? ORDER BY record_id",
                    (draft_id,),
                ).fetchall()
            ]
            return result

        if db is not None:
            return load(db)
        with self.connect() as connection:
            return load(connection)

    def update_draft_status(
        self,
        draft_id: str,
        *,
        status: str,
        confirmed_by: str | None = None,
        error: str | None = None,
        sent_at: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as db:
            current = self.get_draft(draft_id, db=db)
            db.execute(
                """UPDATE summary_drafts SET status=?, confirmed_by=COALESCE(?, confirmed_by),
                   error=?, sent_at=COALESCE(?, sent_at) WHERE id=?""",
                (status, confirmed_by, error, sent_at, draft_id),
            )
            updated = self.get_draft(draft_id, db=db)
            updated["previous_status"] = current["status"]
            return updated

    def add_delivery_log(
        self, draft_id: str, success: bool, response: dict[str, Any]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO delivery_logs VALUES (?, ?, ?, ?, ?)",
                (
                    new_id("delivery"),
                    draft_id,
                    utc_now(),
                    int(success),
                    json.dumps(response, ensure_ascii=False),
                ),
            )

    def get_target(self, alias: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM delivery_targets WHERE alias = ? COLLATE NOCASE AND enabled = 1",
                (alias,),
            ).fetchone()
            if not row:
                raise ValueError(f"unknown or disabled target: {alias}")
            return dict(row)
