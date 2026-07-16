from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from aiohttp import web

from .audit_remote import RemoteNanobotReader
from .audit_store import AuditStore
from .contacts import FeishuDirectory


COOKIE_NAME = "reminder_audit_session"
SESSION_ID_PATTERN = re.compile(r"^feishu_[A-Za-z0-9_.:-]{1,240}$")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _json_response(data: Any, *, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers={"Cache-Control": "no-store"},
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


@dataclass
class AuditApplicationState:
    store: AuditStore
    secret: str
    session_seconds: int
    cookie_secure: bool
    resolve_names: bool
    directory: FeishuDirectory | None = None
    remote: RemoteNanobotReader | None = None
    version: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    scan_task: asyncio.Task[None] | None = None
    failed_logins: dict[str, list[float]] = field(default_factory=dict)
    identity_cache: dict[str, str] = field(default_factory=dict)
    identity_expires_at: float = 0.0

    def sign_cookie(self, expires_at: int) -> str:
        payload = str(expires_at)
        signature = hmac.new(
            self.secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def cookie_valid(self, raw: str | None) -> bool:
        if not raw or "." not in raw:
            return False
        expires_raw, signature = raw.split(".", 1)
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return False
        if expires_at < int(time.time()):
            return False
        expected = self.sign_cookie(expires_at).split(".", 1)[1]
        return hmac.compare_digest(signature, expected)

    async def names(self) -> dict[str, str]:
        if not self.resolve_names or self.directory is None:
            return self.identity_cache
        now = time.monotonic()
        if self.identity_cache and now < self.identity_expires_at:
            return self.identity_cache
        try:
            users = await self.directory.list_users()
        except Exception:
            return self.identity_cache
        cache: dict[str, str] = {}
        for user in users:
            open_id = str(user.get("open_id") or "")
            if not open_id:
                continue
            cache[open_id] = FeishuDirectory._display_name(user)
        self.identity_cache = cache
        self.identity_expires_at = now + 600
        return cache


AUDIT_STATE_KEY = web.AppKey("audit_state", AuditApplicationState)


def _state(request: web.Request) -> AuditApplicationState:
    return request.app[AUDIT_STATE_KEY]


def _peer_id(session_key: str) -> str:
    return session_key.split(":", 1)[1] if ":" in session_key else session_key


def _display_name(session_key: str, names: dict[str, str]) -> str:
    peer = _peer_id(session_key)
    if peer.startswith("ou_"):
        return names.get(peer, f"未知用户 · {peer[-8:]}")
    if peer.startswith("oc_"):
        return f"群聊 · {peer[-8:]}"
    return peer


def _safe_detail(value: Any) -> Any:
    """Redact secret-like keys before internal tool data reaches the browser."""

    sensitive = re.compile(
        r"(?:secret|token|password|authorization|api[_-]?key|app[_-]?secret)",
        flags=re.IGNORECASE,
    )
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if sensitive.search(str(key)) else _safe_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_detail(item) for item in value]
    return value


@web.middleware
async def security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@web.middleware
async def authentication(request: web.Request, handler: Any) -> web.StreamResponse:
    public = {
        "/",
        "/favicon.ico",
        "/health",
        "/api/auth",
        "/api/login",
        "/static/app.css",
        "/static/app.js",
    }
    if request.path in public:
        return await handler(request)
    if not _state(request).cookie_valid(request.cookies.get(COOKIE_NAME)):
        return _json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def index(request: web.Request) -> web.FileResponse:
    path = Path(str(files("reminder_mcp").joinpath("audit_static", "index.html")))
    return web.FileResponse(path)


async def health(request: web.Request) -> web.Response:
    return _json_response({"status": "ok"})


async def favicon(request: web.Request) -> web.Response:
    return web.Response(status=204, headers={"Cache-Control": "public, max-age=86400"})


async def auth_status(request: web.Request) -> web.Response:
    authenticated = _state(request).cookie_valid(request.cookies.get(COOKIE_NAME))
    return _json_response({"authenticated": authenticated})


def _login_limited(state: AuditApplicationState, remote: str) -> bool:
    now = time.monotonic()
    recent = [value for value in state.failed_logins.get(remote, []) if now - value < 60]
    state.failed_logins[remote] = recent
    return len(recent) >= 5


async def login(request: web.Request) -> web.Response:
    state = _state(request)
    remote = request.remote or "unknown"
    if _login_limited(state, remote):
        return _json_response({"error": "too_many_attempts"}, status=429)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_response({"error": "invalid_request"}, status=400)
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or not hmac.compare_digest(password, state.secret):
        state.failed_logins.setdefault(remote, []).append(time.monotonic())
        await asyncio.sleep(0.25)
        return _json_response({"error": "invalid_credentials"}, status=401)
    state.failed_logins.pop(remote, None)
    expires_at = int(time.time()) + state.session_seconds
    response = _json_response({"authenticated": True})
    response.set_cookie(
        COOKIE_NAME,
        state.sign_cookie(expires_at),
        max_age=state.session_seconds,
        httponly=True,
        secure=state.cookie_secure,
        samesite="Strict",
        path="/",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    response = _json_response({"authenticated": False})
    response.del_cookie(COOKIE_NAME, path="/")
    return response


async def overview(request: web.Request) -> web.Response:
    state = _state(request)
    data = await asyncio.to_thread(state.store.overview)
    data["remote"] = state.remote.status if state.remote is not None else {
        "enabled": False,
        "connected": True,
    }
    return _json_response(data)


async def conversations(request: web.Request) -> web.Response:
    state = _state(request)
    rows, names = await asyncio.gather(
        asyncio.to_thread(state.store.conversations), state.names()
    )
    query = request.query.get("q", "").strip().casefold()
    result = []
    for row in rows:
        item = dict(row)
        session_key = str(item["session_key"])
        item["peer_id"] = _peer_id(session_key)
        item["display_name"] = _display_name(session_key, names)
        item.pop("metadata_json", None)
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("display_name", "peer_id", "last_content")
        ).casefold()
        if query and query not in haystack:
            continue
        result.append(item)
    return _json_response({"conversations": result, "count": len(result)})


async def conversation_detail(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        return _json_response({"error": "invalid_session"}, status=400)
    try:
        limit = int(request.query.get("limit", "2000"))
    except ValueError:
        return _json_response({"error": "invalid_limit"}, status=400)
    state = _state(request)
    detail, names = await asyncio.gather(
        asyncio.to_thread(state.store.conversation, session_id, limit=limit),
        state.names(),
    )
    if detail is None:
        return _json_response({"error": "not_found"}, status=404)
    detail["display_name"] = _display_name(str(detail["session_key"]), names)
    detail["peer_id"] = _peer_id(str(detail["session_key"]))
    for event in detail["events"]:
        event["tool_calls"] = _safe_detail(event.get("tool_calls") or [])
        event["raw"] = _safe_detail(event.get("raw") or {})
    for upload in detail["uploads"]:
        upload["content_url"] = (
            f"/api/uploads/{upload['upload_id']}/content"
            if upload.get("status") == "available"
            else None
        )
    return _json_response(detail)


async def uploads(request: web.Request) -> web.Response:
    state = _state(request)
    rows, names = await asyncio.gather(
        asyncio.to_thread(state.store.uploads), state.names()
    )
    status = request.query.get("status", "").strip()
    result = []
    for row in rows:
        if status and row.get("status") != status:
            continue
        item = dict(row)
        session_key = item.get("session_key")
        item["display_name"] = (
            _display_name(str(session_key), names) if session_key else "未关联文件"
        )
        item["content_url"] = (
            f"/api/uploads/{item['upload_id']}/content"
            if item.get("status") == "available"
            else None
        )
        result.append(item)
    return _json_response({"uploads": result, "count": len(result)})


async def upload_content(request: web.Request) -> web.StreamResponse:
    upload_id = request.match_info["upload_id"]
    if re.fullmatch(r"[a-f0-9]{64}", upload_id) is None:
        return _json_response({"error": "invalid_upload"}, status=400)
    found = await asyncio.to_thread(_state(request).store.upload_file, upload_id)
    if found is None:
        return _json_response({"error": "not_found"}, status=404)
    path, filename = found
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    inline = mime in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "audio/mpeg",
        "audio/ogg",
        "video/mp4",
        "application/pdf",
    }
    response = web.FileResponse(path, headers={"Cache-Control": "no-store"})
    response.content_type = mime
    disposition = "inline" if inline else "attachment"
    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
    response.headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    return response


async def events(request: web.Request) -> web.StreamResponse:
    state = _state(request)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    version = state.version
    await response.write(f"event: ready\ndata: {{\"version\":{version}}}\n\n".encode())
    try:
        while True:
            async with state.condition:
                try:
                    await asyncio.wait_for(
                        state.condition.wait_for(lambda: state.version != version),
                        timeout=15,
                    )
                except TimeoutError:
                    await response.write(b": heartbeat\n\n")
                    continue
                version = state.version
            await response.write(
                f"id: {version}\nevent: changed\ndata: {{\"version\":{version}}}\n\n".encode()
            )
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def _scan_loop(state: AuditApplicationState) -> None:
    while True:
        try:
            remote_changed = False
            if state.remote is not None:
                remote_changed = await state.remote.sync_once()
            changed = await asyncio.to_thread(state.store.refresh)
            if changed or remote_changed:
                async with state.condition:
                    state.version += 1
                    state.condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately avoid logging source content or secrets. A later scan
            # retries, and /health remains available for container supervision.
            pass
        await asyncio.sleep(1)


async def _startup(app: web.Application) -> None:
    state = app[AUDIT_STATE_KEY]
    if state.remote is not None:
        await state.remote.sync_once()
    await asyncio.to_thread(state.store.refresh)
    state.scan_task = asyncio.create_task(_scan_loop(state))


async def _cleanup(app: web.Application) -> None:
    state = app[AUDIT_STATE_KEY]
    if state.scan_task is not None:
        state.scan_task.cancel()
        await asyncio.gather(state.scan_task, return_exceptions=True)
    if state.directory is not None:
        await state.directory.close()
    if state.remote is not None:
        await state.remote.close()


def create_app() -> web.Application:
    database_path = Path(
        os.getenv("AUDIT_DB_PATH", "/var/lib/reminder-audit/audit.db")
    )
    secret = (os.getenv("AUDIT_ADMIN_TOKEN") or os.getenv("NANOBOT_WEBUI_SECRET") or "").strip()
    if len(secret) < 32:
        raise RuntimeError("AUDIT_ADMIN_TOKEN must contain at least 32 characters")
    session_seconds = max(
        300, min(int(os.getenv("AUDIT_SESSION_SECONDS", "43200")), 86_400)
    )
    resolve_names = _env_bool("AUDIT_RESOLVE_FEISHU_NAMES", True)
    directory = FeishuDirectory() if resolve_names else None
    remote_url = os.getenv("AUDIT_REMOTE_URL", "").strip()
    if remote_url:
        state_path = Path(
            os.getenv(
                "AUDIT_REMOTE_CACHE_PATH",
                str(database_path.parent / "remote-source"),
            )
        )
        remote_secret = (
            os.getenv("AUDIT_REMOTE_SECRET")
            or os.getenv("NANOBOT_WEBUI_SECRET")
            or ""
        ).strip()
        if len(remote_secret) < 32:
            raise RuntimeError("AUDIT_REMOTE_SECRET must contain at least 32 characters")
        remote = RemoteNanobotReader(
            base_url=remote_url,
            secret=remote_secret,
            target_state_path=state_path,
            directory=directory,
            allow_insecure=_env_bool("AUDIT_ALLOW_INSECURE_REMOTE", False),
        )
    else:
        state_path = Path(os.getenv("AUDIT_STATE_PATH", str(Path.home() / ".nanobot")))
        remote = None
    state = AuditApplicationState(
        store=AuditStore(state_path, database_path),
        secret=secret,
        session_seconds=session_seconds,
        cookie_secure=_env_bool("AUDIT_COOKIE_SECURE", False),
        resolve_names=resolve_names,
        directory=directory,
        remote=remote,
    )
    app = web.Application(middlewares=[security_headers, authentication])
    app[AUDIT_STATE_KEY] = state
    static_path = Path(str(files("reminder_mcp").joinpath("audit_static")))
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/health", health)
    app.router.add_get("/api/auth", auth_status)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_get("/api/conversations/{session_id}", conversation_detail)
    app.router.add_get("/api/uploads", uploads)
    app.router.add_get("/api/uploads/{upload_id}/content", upload_content)
    app.router.add_get("/api/events", events)
    app.router.add_static("/static", static_path, show_index=False)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


def main() -> None:
    host = os.getenv("AUDIT_HOST", "0.0.0.0")
    port = int(os.getenv("AUDIT_PORT", "8780"))
    web.run_app(create_app(), host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
