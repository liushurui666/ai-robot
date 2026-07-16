from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_MEDIA_PATTERN = re.compile(
    r"\[(image|file|audio|media):\s*([^\]]+)\]",
    flags=re.IGNORECASE,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(session_key: str, row: dict[str, Any]) -> str:
    payload = f"{session_key}\0{_json(row)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _session_key_from_path(path: Path) -> str | None:
    stem = path.stem
    if not stem.startswith("feishu_"):
        return None
    return f"feishu:{stem.removeprefix('feishu_')}"


def _session_id(session_key: str) -> str:
    return session_key.replace(":", "_", 1)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except (FileNotFoundError, OSError):
        return []
    return rows


class AuditStore:
    """Append-only audit projection of nanobot's mutable session files.

    nanobot compacts idle sessions. This store imports every visible row into a
    separate SQLite database so rows already observed remain queryable after
    the source session is compacted.
    """

    def __init__(self, state_path: Path, database_path: Path):
        self.state_path = state_path.resolve()
        self.workspace_path = self.state_path / "workspace"
        self.sessions_path = self.workspace_path / "sessions"
        self.history_path = self.workspace_path / "memory" / "history.jsonl"
        self.media_path = (self.state_path / "media" / "feishu").resolve()
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS audit_sessions (
                    session_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    created_at TEXT,
                    updated_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL REFERENCES audit_sessions(session_key),
                    timestamp TEXT,
                    role TEXT,
                    name TEXT,
                    content TEXT NOT NULL DEFAULT '',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    row_json TEXT NOT NULL,
                    is_cron INTEGER NOT NULL DEFAULT 0,
                    source_order INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_events_session_time
                    ON audit_events(session_key, timestamp, first_seen_at);

                CREATE TABLE IF NOT EXISTS audit_summaries (
                    summary_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL REFERENCES audit_sessions(session_key),
                    cursor INTEGER,
                    timestamp TEXT,
                    content TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_summaries_session_time
                    ON audit_summaries(session_key, timestamp);

                CREATE TABLE IF NOT EXISTS audit_uploads (
                    upload_id TEXT PRIMARY KEY,
                    event_id TEXT REFERENCES audit_events(event_id),
                    session_key TEXT REFERENCES audit_sessions(session_key),
                    timestamp TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    filename TEXT,
                    media_path TEXT,
                    error TEXT,
                    first_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_uploads_session_time
                    ON audit_uploads(session_key, timestamp);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def refresh(self) -> bool:
        """Import new session rows, summaries, and media records.

        Returns True when the audit projection changed.
        """

        with self._lock, self._connect() as db:
            before = db.total_changes
            now = self._now()
            if not self.sessions_path.is_dir():
                return False
            for path in sorted(self.sessions_path.glob("feishu_*.jsonl")):
                self._import_session(db, path, now)
            self._import_history(db, now)
            self._import_attachment_manifest(db, now)
            self._import_orphan_media(db, now)
            return db.total_changes > before

    def _import_session(self, db: sqlite3.Connection, path: Path, now: str) -> None:
        rows = _read_jsonl(path)
        metadata_row = next((row for row in rows if row.get("_type") == "metadata"), {})
        metadata_key = metadata_row.get("key")
        session_key = (
            metadata_key
            if isinstance(metadata_key, str) and metadata_key.startswith("feishu:")
            else _session_key_from_path(path)
        )
        if session_key is None:
            return
        created_at = metadata_row.get("created_at")
        updated_at = metadata_row.get("updated_at")
        metadata = metadata_row.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        db.execute(
            """INSERT INTO audit_sessions
               (session_key, session_id, created_at, updated_at, metadata_json,
                first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_key) DO UPDATE SET
                 created_at=COALESCE(excluded.created_at, audit_sessions.created_at),
                 updated_at=COALESCE(excluded.updated_at, audit_sessions.updated_at),
                 metadata_json=excluded.metadata_json,
                 last_seen_at=excluded.last_seen_at
               WHERE excluded.created_at IS NOT audit_sessions.created_at
                  OR excluded.updated_at IS NOT audit_sessions.updated_at
                  OR excluded.metadata_json IS NOT audit_sessions.metadata_json""",
            (
                session_key,
                _session_id(session_key),
                created_at,
                updated_at,
                _json(metadata),
                now,
                now,
            ),
        )

        for source_order, row in enumerate(rows):
            if row.get("_type") == "metadata":
                continue
            event_id = _event_id(session_key, row)
            content = row.get("content")
            if not isinstance(content, str):
                content = _json(content) if content is not None else ""
            tool_calls = row.get("tool_calls")
            if not isinstance(tool_calls, list):
                tool_calls = []
            cursor = db.execute(
                """INSERT OR IGNORE INTO audit_events
                   (event_id, session_key, timestamp, role, name, content,
                    tool_calls_json, row_json, is_cron, source_order, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    session_key,
                    row.get("timestamp"),
                    row.get("role"),
                    row.get("name"),
                    content,
                    _json(tool_calls),
                    _json(row),
                    1 if row.get("_cron_turn") is True else 0,
                    source_order,
                    now,
                ),
            )
            if cursor.rowcount:
                self._import_uploads_for_event(
                    db,
                    event_id=event_id,
                    session_key=session_key,
                    timestamp=row.get("timestamp"),
                    content=content,
                    media=row.get("media"),
                    now=now,
                )

    def _import_history(self, db: sqlite3.Connection, now: str) -> None:
        for row in _read_jsonl(self.history_path):
            session_key = row.get("session_key")
            if not isinstance(session_key, str) or not session_key.startswith("feishu:"):
                continue
            exists = db.execute(
                "SELECT 1 FROM audit_sessions WHERE session_key=?",
                (session_key,),
            ).fetchone()
            if exists is None:
                db.execute(
                    """INSERT INTO audit_sessions
                       (session_key, session_id, created_at, updated_at,
                        metadata_json, first_seen_at, last_seen_at)
                       VALUES (?, ?, NULL, ?, '{}', ?, ?)""",
                    (session_key, _session_id(session_key), row.get("timestamp"), now, now),
                )
            summary_id = hashlib.sha256(
                f"{session_key}\0{row.get('cursor')}\0{row.get('timestamp')}\0{row.get('content')}".encode()
            ).hexdigest()
            db.execute(
                """INSERT OR IGNORE INTO audit_summaries
                   (summary_id, session_key, cursor, timestamp, content, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    summary_id,
                    session_key,
                    row.get("cursor"),
                    row.get("timestamp"),
                    str(row.get("content") or ""),
                    now,
                ),
            )

    def _resolve_media_path(self, raw: str) -> Path | None:
        raw_path = Path(raw.strip())
        candidate = raw_path if raw_path.is_absolute() else self.workspace_path / raw_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.media_path)
        except (FileNotFoundError, OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _import_uploads_for_event(
        self,
        db: sqlite3.Connection,
        *,
        event_id: str,
        session_key: str,
        timestamp: Any,
        content: str,
        media: Any,
        now: str,
    ) -> None:
        candidates = [
            (match.group(1).lower(), match.group(2).strip())
            for match in _MEDIA_PATTERN.finditer(content)
        ]
        if isinstance(media, list):
            for raw_path in media:
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                suffix = Path(raw_path).suffix.casefold()
                kind = "image" if suffix in {
                    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"
                } else "file"
                candidate = (kind, raw_path.strip())
                if candidate not in candidates:
                    candidates.append(candidate)
        for ordinal, (kind, detail) in enumerate(candidates):
            lowered = detail.casefold()
            resolved = self._resolve_media_path(detail)
            if resolved is not None:
                status = "available"
                filename = resolved.name
                media_path = str(resolved)
                error = None
            else:
                status = "failed" if any(
                    token in lowered for token in ("failed", "missing", "rejected", "error")
                ) else "missing"
                filename = Path(detail).name if "/" in detail else None
                media_path = None
                error = detail
            upload_id = hashlib.sha256(
                f"{event_id}\0{ordinal}\0{kind}\0{detail}".encode()
            ).hexdigest()
            db.execute(
                """INSERT OR IGNORE INTO audit_uploads
                   (upload_id, event_id, session_key, timestamp, kind, status,
                    filename, media_path, error, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    upload_id,
                    event_id,
                    session_key,
                    timestamp,
                    kind,
                    status,
                    filename,
                    media_path,
                    error,
                    now,
                ),
            )

    def _import_orphan_media(self, db: sqlite3.Connection, now: str) -> None:
        if not self.media_path.is_dir():
            return
        for path in self.media_path.rglob("*"):
            if not path.is_file() or path.name == "audit-manifest.jsonl":
                continue
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(self.media_path).as_posix()
            except (FileNotFoundError, OSError, ValueError):
                continue
            known = db.execute(
                "SELECT 1 FROM audit_uploads WHERE media_path=?",
                (str(resolved),),
            ).fetchone()
            if known is not None:
                continue
            upload_id = hashlib.sha256(f"orphan\0{relative}".encode()).hexdigest()
            kind = "image" if resolved.suffix.casefold() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"
            } else "file"
            db.execute(
                """INSERT OR IGNORE INTO audit_uploads
                   (upload_id, event_id, session_key, timestamp, kind, status,
                    filename, media_path, error, first_seen_at)
                   VALUES (?, NULL, NULL, ?, ?, 'available', ?, ?, NULL, ?)""",
                (
                    upload_id,
                    datetime.fromtimestamp(resolved.stat().st_mtime, UTC).isoformat(),
                    kind,
                    resolved.name,
                    str(resolved),
                    now,
                ),
            )

    def _import_attachment_manifest(self, db: sqlite3.Connection, now: str) -> None:
        """Associate locally recovered Feishu resources with their user event."""

        manifest = self.media_path / "audit-manifest.jsonl"
        for row in _read_jsonl(manifest):
            session_key = row.get("session_key")
            timestamp = row.get("timestamp")
            kind = str(row.get("kind") or "file")
            raw_path = row.get("path")
            if (
                not isinstance(session_key, str)
                or not isinstance(timestamp, str)
                or not isinstance(raw_path, str)
            ):
                continue
            resolved = self._resolve_media_path(raw_path)
            if resolved is None:
                continue
            event = db.execute(
                """SELECT event_id FROM audit_events
                   WHERE session_key=? AND timestamp=? AND role='user'
                   ORDER BY source_order LIMIT 1""",
                (session_key, timestamp),
            ).fetchone()
            if event is None:
                continue
            event_id = str(event["event_id"])
            existing = db.execute(
                "SELECT 1 FROM audit_uploads WHERE event_id=? AND media_path=?",
                (event_id, str(resolved)),
            ).fetchone()
            if existing is not None:
                continue
            unresolved = db.execute(
                """SELECT upload_id FROM audit_uploads
                   WHERE event_id=? AND kind=? AND status<>'available'
                   ORDER BY first_seen_at, upload_id LIMIT 1""",
                (event_id, kind),
            ).fetchone()
            filename = str(row.get("filename") or resolved.name)
            if unresolved is not None:
                db.execute(
                    """UPDATE audit_uploads
                       SET status='available', filename=?, media_path=?, error=NULL
                       WHERE upload_id=?""",
                    (filename, str(resolved), unresolved["upload_id"]),
                )
                continue
            upload_id = hashlib.sha256(
                f"feishu-resource\0{row.get('message_id')}\0{row.get('file_key')}".encode()
            ).hexdigest()
            db.execute(
                """INSERT OR IGNORE INTO audit_uploads
                   (upload_id, event_id, session_key, timestamp, kind, status,
                    filename, media_path, error, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, 'available', ?, ?, NULL, ?)""",
                (
                    upload_id,
                    event_id,
                    session_key,
                    timestamp,
                    kind,
                    filename,
                    str(resolved),
                    now,
                ),
            )

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def overview(self) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            sessions = db.execute("SELECT COUNT(*) FROM audit_sessions").fetchone()[0]
            human_messages = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE role='user' AND is_cron=0"
            ).fetchone()[0]
            assistant_messages = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE role='assistant' AND content<>''"
            ).fetchone()[0]
            uploads = db.execute("SELECT COUNT(*) FROM audit_uploads").fetchone()[0]
            failed_uploads = db.execute(
                "SELECT COUNT(*) FROM audit_uploads WHERE status<>'available'"
            ).fetchone()[0]
            latest = db.execute(
                "SELECT MAX(COALESCE(timestamp, first_seen_at)) FROM audit_events"
            ).fetchone()[0]
        return {
            "conversations": sessions,
            "human_messages": human_messages,
            "assistant_messages": assistant_messages,
            "uploads": uploads,
            "failed_uploads": failed_uploads,
            "latest_event_at": latest,
        }

    def conversations(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT s.*,
                          COUNT(e.event_id) AS event_count,
                          SUM(CASE WHEN e.role='user' AND e.is_cron=0 THEN 1 ELSE 0 END) AS human_messages,
                          SUM(CASE WHEN e.role='assistant' AND e.content<>'' THEN 1 ELSE 0 END) AS assistant_messages,
                          MAX(COALESCE(e.timestamp, e.first_seen_at)) AS latest_event_at,
                          (SELECT content FROM audit_events last
                            WHERE last.session_key=s.session_key AND last.content<>''
                            ORDER BY COALESCE(last.timestamp, last.first_seen_at) DESC LIMIT 1
                          ) AS last_content,
                          (SELECT COUNT(*) FROM audit_uploads u
                            WHERE u.session_key=s.session_key
                          ) AS upload_count,
                          (SELECT COUNT(*) FROM audit_uploads u
                            WHERE u.session_key=s.session_key AND u.status<>'available'
                          ) AS failed_uploads
                   FROM audit_sessions s
                   LEFT JOIN audit_events e ON e.session_key=s.session_key
                   GROUP BY s.session_key
                   ORDER BY COALESCE(latest_event_at, s.updated_at, s.last_seen_at) DESC"""
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def conversation(self, session_id: str, *, limit: int = 2000) -> dict[str, Any] | None:
        limit = max(1, min(limit, 5000))
        with self._lock, self._connect() as db:
            session = db.execute(
                "SELECT * FROM audit_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                return None
            total = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE session_key=?",
                (session["session_key"],),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT * FROM (
                       SELECT * FROM audit_events WHERE session_key=?
                       ORDER BY COALESCE(timestamp, first_seen_at) DESC, source_order DESC, first_seen_at DESC
                       LIMIT ?
                   ) ORDER BY COALESCE(timestamp, first_seen_at), source_order, first_seen_at""",
                (session["session_key"], limit),
            ).fetchall()
            summaries = db.execute(
                """SELECT cursor, timestamp, content FROM audit_summaries
                   WHERE session_key=? ORDER BY timestamp, cursor""",
                (session["session_key"],),
            ).fetchall()
            uploads = db.execute(
                """SELECT upload_id, event_id, timestamp, kind, status, filename, error
                   FROM audit_uploads WHERE session_key=? ORDER BY timestamp""",
                (session["session_key"],),
            ).fetchall()
        events = []
        for row in rows:
            item = self._row_dict(row)
            item["tool_calls"] = json.loads(item.pop("tool_calls_json") or "[]")
            item["raw"] = json.loads(item.pop("row_json") or "{}")
            events.append(item)
        result = self._row_dict(session)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["events"] = events
        result["event_total"] = total
        result["truncated"] = total > len(events)
        result["summaries"] = [self._row_dict(row) for row in summaries]
        result["uploads"] = [self._row_dict(row) for row in uploads]
        return result

    def uploads(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT upload_id, event_id, session_key, timestamp, kind,
                          status, filename, error, first_seen_at
                   FROM audit_uploads
                   ORDER BY COALESCE(timestamp, first_seen_at) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def upload_file(self, upload_id: str) -> tuple[Path, str] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT media_path, filename, status FROM audit_uploads WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
        if row is None or row["status"] != "available" or not row["media_path"]:
            return None
        try:
            path = Path(row["media_path"]).resolve(strict=True)
            path.relative_to(self.media_path)
        except (FileNotFoundError, OSError, ValueError):
            return None
        return path, str(row["filename"] or path.name)
