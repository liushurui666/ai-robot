from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .contacts import FeishuDirectory, FeishuMessagePermissionError


_SESSION_KEY_PATTERN = re.compile(r"^feishu:[A-Za-z0-9_.:-]{1,240}$")


class RemoteNanobotReader:
    """Read-only collector for a remote nanobot WebUI.

    The collector runs only in the local audit tool. It uses the existing
    authenticated file-preview route to copy conversation JSONL into a local
    staging directory; it never writes to the remote server.
    """

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        target_state_path: Path,
        directory: FeishuDirectory | None,
        allow_insecure: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AUDIT_REMOTE_URL must be an http(s) URL")
        if (
            parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            and not allow_insecure
        ):
            raise ValueError(
                "remote HTTP is unencrypted; set AUDIT_ALLOW_INSECURE_REMOTE=true to opt in"
            )
        if parsed.query or parsed.fragment:
            raise ValueError("AUDIT_REMOTE_URL must not contain query or fragment")
        self.base_url = base_url.rstrip("/")
        self.secure_transport = parsed.scheme == "https" or parsed.hostname in {
            "127.0.0.1", "localhost", "::1"
        }
        self.secret = secret
        self.target_state_path = target_state_path.resolve()
        self.sessions_path = self.target_state_path / "workspace" / "sessions"
        self.history_path = self.target_state_path / "workspace" / "memory" / "history.jsonl"
        self.cron_path = self.target_state_path / "workspace" / "cron" / "jobs.json"
        self.media_path = self.target_state_path / "media" / "feishu"
        self.media_manifest_path = self.media_path / "audit-manifest.jsonl"
        self.directory = directory
        self.client = client or httpx.AsyncClient(timeout=25, follow_redirects=False)
        self._owns_client = client is None
        self._api_token = ""
        self._api_token_expires_at = 0.0
        self._known_session_keys: set[str] = set()
        self._last_discovery_at = 0.0
        self._last_known_sync_at = 0.0
        self._last_media_sync_at = 0.0
        self._checked_media_messages: set[str] = set()
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self.media_last_success_at: float | None = None
        self.media_last_error: str | None = None
        self.media_permission_required = False
        self.truncated_files: set[str] = set()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "connected": self.last_success_at is not None and self.last_error is None,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "known_sessions": len(self._known_session_keys),
            "truncated_files": len(self.truncated_files),
            "secure_transport": self.secure_transport,
            "media_connected": self.media_last_success_at is not None
            and self.media_last_error is None,
            "media_last_success_at": self.media_last_success_at,
            "media_last_error": self.media_last_error,
            "media_permission_required": self.media_permission_required,
        }

    async def _token(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._api_token and now < self._api_token_expires_at - 15:
            return self._api_token
        response = await self.client.get(
            f"{self.base_url}/webui/bootstrap",
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("remote bootstrap did not return an API token")
        self._api_token = token
        self._api_token_expires_at = now + max(int(data.get("expires_in", 300)), 30)
        return token

    async def _preview(self, path: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,500}", path) or ".." in path:
            raise ValueError("invalid remote preview path")
        for attempt in range(2):
            token = await self._token(force=attempt == 1)
            response = await self.client.get(
                f"{self.base_url}/api/sessions/websocket:audit-reader/file-preview",
                params={"path": path},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 404:
                return None
            if response.status_code == 401 and attempt == 0:
                continue
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        return None

    @staticmethod
    def _safe_session_filename(session_key: str) -> str:
        if _SESSION_KEY_PATTERN.fullmatch(session_key) is None:
            raise ValueError("invalid Feishu session key")
        return f"{session_key.replace(':', '_')}.jsonl"

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        return RemoteNanobotReader._write_bytes_if_changed(path, content.encode("utf-8"))

    @staticmethod
    def _write_bytes_if_changed(path: Path, encoded: bytes) -> bool:
        try:
            if path.read_bytes() == encoded:
                return False
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(encoded)
        os.replace(tmp, path)
        return True

    def _discover_from_cached_files(self) -> set[str]:
        found: set[str] = set()
        try:
            with self.history_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = row.get("session_key") if isinstance(row, dict) else None
                    if isinstance(key, str) and _SESSION_KEY_PATTERN.fullmatch(key):
                        found.add(key)
        except (FileNotFoundError, OSError):
            pass
        try:
            raw = json.loads(self.cron_path.read_text(encoding="utf-8"))
            jobs = raw.get("jobs", []) if isinstance(raw, dict) else []
            for job in jobs:
                payload = job.get("payload", {}) if isinstance(job, dict) else {}
                key = payload.get("sessionKey") if isinstance(payload, dict) else None
                if isinstance(key, str) and _SESSION_KEY_PATTERN.fullmatch(key):
                    found.add(key)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return found

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
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
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip().replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def _nearest_user_timestamp(
        self, session_key: str, created_at: datetime
    ) -> str | None:
        try:
            path = self.sessions_path / self._safe_session_filename(session_key)
        except ValueError:
            return None
        best: tuple[float, str] | None = None
        for row in self._read_jsonl(path):
            if row.get("role") != "user" or row.get("_cron_turn") is True:
                continue
            parsed = self._parse_timestamp(row.get("timestamp"))
            if parsed is None:
                continue
            delta = (created_at - parsed).total_seconds()
            if delta < -60 or delta > 600:
                continue
            candidate = (abs(delta), str(row.get("timestamp")))
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best[1] if best else None

    def _discover_media_references(self) -> dict[str, dict[str, str | None]]:
        references: dict[str, dict[str, str | None]] = {}
        try:
            payload = json.loads(self.cron_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        for job in payload.get("jobs", []) if isinstance(payload, dict) else []:
            if not isinstance(job, dict):
                continue
            body = job.get("payload")
            metadata = body.get("originMetadata") if isinstance(body, dict) else None
            message_id = metadata.get("message_id") if isinstance(metadata, dict) else None
            session_key = body.get("sessionKey") if isinstance(body, dict) else None
            if (
                not isinstance(message_id, str)
                or re.fullmatch(r"om_[A-Za-z0-9]+", message_id) is None
                or not isinstance(session_key, str)
                or _SESSION_KEY_PATTERN.fullmatch(session_key) is None
            ):
                continue
            created_ms = job.get("createdAtMs")
            created_at = (
                datetime.fromtimestamp(float(created_ms) / 1000, UTC)
                if isinstance(created_ms, (int, float))
                else None
            )
            references[message_id] = {
                "message_id": message_id,
                "session_key": session_key,
                "timestamp": self._nearest_user_timestamp(session_key, created_at)
                if created_at
                else None,
            }

        # Future nanobot versions may persist inbound message metadata directly
        # on the user row. Prefer that exact association when it is present.
        for path in self.sessions_path.glob("feishu_*.jsonl"):
            session_key = None
            for row in self._read_jsonl(path):
                if row.get("_type") == "metadata":
                    key = row.get("key")
                    if isinstance(key, str) and _SESSION_KEY_PATTERN.fullmatch(key):
                        session_key = key
                    continue
                metadata = row.get("metadata")
                message_id = metadata.get("message_id") if isinstance(metadata, dict) else None
                if (
                    session_key
                    and isinstance(message_id, str)
                    and re.fullmatch(r"om_[A-Za-z0-9]+", message_id)
                ):
                    references[message_id] = {
                        "message_id": message_id,
                        "session_key": session_key,
                        "timestamp": str(row.get("timestamp") or "") or None,
                    }
        return references

    @staticmethod
    def _safe_media_name(name: str | None, *, kind: str, ordinal: int) -> str:
        candidate = Path((name or "").replace("\\", "/")).name
        candidate = re.sub(r"[^A-Za-z0-9._()\-\u4e00-\u9fff]+", "-", candidate).strip(".-")
        if candidate:
            return candidate[:180]
        return f"attachment-{ordinal}.jpg" if kind == "image" else f"attachment-{ordinal}.bin"

    async def _sync_feishu_media(self) -> bool:
        if self.directory is None or not hasattr(self.directory, "message_attachments"):
            return False
        manifest_rows = self._read_jsonl(self.media_manifest_path)
        manifest_keys = {
            (str(row.get("message_id") or ""), str(row.get("file_key") or ""))
            for row in manifest_rows
        }
        self._checked_media_messages.update(
            str(row.get("message_id")) for row in manifest_rows if row.get("message_id")
        )
        changed = False
        try:
            for message_id, reference in self._discover_media_references().items():
                if message_id in self._checked_media_messages:
                    continue
                attachments = await self.directory.message_attachments(message_id)
                for ordinal, attachment in enumerate(attachments):
                    file_key = str(attachment.get("file_key") or "")
                    if not file_key or (message_id, file_key) in manifest_keys:
                        continue
                    kind = str(attachment.get("kind") or "file")
                    data, response_name = await self.directory.download_message_attachment(
                        message_id, file_key, kind
                    )
                    filename = self._safe_media_name(
                        str(attachment.get("filename") or response_name or ""),
                        kind=kind,
                        ordinal=ordinal,
                    )
                    target = self.media_path / f"{message_id[-12:]}-{ordinal}-{filename}"
                    self._write_bytes_if_changed(target, data)
                    manifest_rows.append(
                        {
                            **reference,
                            "file_key": file_key,
                            "kind": kind,
                            "filename": filename,
                            "path": str(target.resolve()),
                        }
                    )
                    manifest_keys.add((message_id, file_key))
                    changed = True
                self._checked_media_messages.add(message_id)
            if changed:
                manifest_text = "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in manifest_rows
                )
                self._write_if_changed(self.media_manifest_path, manifest_text)
            self.media_last_success_at = time.time()
            self.media_last_error = None
            self.media_permission_required = False
            return changed
        except FeishuMessagePermissionError:
            self.media_last_error = "permission_required"
            self.media_permission_required = True
            return False
        except Exception as exc:
            self.media_last_error = type(exc).__name__
            return False

    async def _discover_user_sessions(self) -> set[str]:
        if self.directory is None:
            return set()
        users = await self.directory.list_users()
        return {
            f"feishu:{open_id}"
            for user in users
            if (open_id := str(user.get("open_id") or ""))
            and re.fullmatch(r"ou_[A-Za-z0-9]+", open_id)
        }

    async def _sync_shared_files(self) -> bool:
        changed = False
        for remote_path, local_path in (
            ("memory/history.jsonl", self.history_path),
            ("cron/jobs.json", self.cron_path),
        ):
            data = await self._preview(remote_path)
            if data is None:
                continue
            if data.get("truncated") is True:
                self.truncated_files.add(remote_path)
            content = data.get("content")
            if isinstance(content, str):
                changed = self._write_if_changed(local_path, content) or changed
        return changed

    async def _sync_session(self, session_key: str, semaphore: asyncio.Semaphore) -> bool:
        filename = self._safe_session_filename(session_key)
        async with semaphore:
            data = await self._preview(f"sessions/{filename}")
        if data is None:
            return False
        if data.get("truncated") is True:
            self.truncated_files.add(filename)
        content = data.get("content")
        if not isinstance(content, str):
            return False
        # Never replace a previously complete local copy with a truncated
        # remote prefix. The append-only audit projection already contains the
        # rows observed before the remote file crossed the preview limit.
        target = self.sessions_path / filename
        if data.get("truncated") is True and target.exists():
            return False
        return self._write_if_changed(target, content)

    async def sync_once(self) -> bool:
        now = time.monotonic()
        discovery_due = now - self._last_discovery_at >= 60
        known_due = now - self._last_known_sync_at >= 3
        media_due = now - self._last_media_sync_at >= 60
        if not discovery_due and not known_due and not media_due:
            return False
        try:
            changed = await self._sync_shared_files()
            self._known_session_keys.update(self._discover_from_cached_files())
            if discovery_due:
                self._known_session_keys.update(await self._discover_user_sessions())
                self._last_discovery_at = now
            if known_due:
                semaphore = asyncio.Semaphore(8)
                results = await asyncio.gather(
                    *(self._sync_session(key, semaphore) for key in sorted(self._known_session_keys)),
                    return_exceptions=True,
                )
                changed = any(result is True for result in results) or changed
                self._last_known_sync_at = now
            if media_due:
                changed = await self._sync_feishu_media() or changed
                self._last_media_sync_at = now
            self.last_success_at = time.time()
            self.last_error = None
            return changed
        except Exception as exc:
            self.last_error = type(exc).__name__
            return False
