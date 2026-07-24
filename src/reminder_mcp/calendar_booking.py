from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .contacts import FeishuDirectory


class FeishuAPIError(RuntimeError):
    def __init__(self, operation: str, code: Any, message: str):
        self.operation = operation
        self.code = code
        self.message = message
        super().__init__(f"{operation} failed ({code}): {message}")


class FeishuCalendarBooker:
    """Resolve people and rooms, check availability, then create a Feishu event."""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=20)
        self._owns_client = client is None
        self.directory = FeishuDirectory(self.client)
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"required environment variable is not set: {name}")
        return value

    async def _tenant_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        response = await self.client.post(
            f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._required_env("FEISHU_APP_ID"),
                "app_secret": self._required_env("FEISHU_APP_SECRET"),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise FeishuAPIError("获取 tenant_access_token", data.get("code"), str(data.get("msg")))
        self._token = str(data["tenant_access_token"])
        self._token_expires_at = now + max(int(data.get("expire", 7200)) - 60, 60)
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._tenant_token()
        response = await self.client.request(
            method,
            f"{self.BASE_URL}{path}",
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise FeishuAPIError(operation, response.status_code, "invalid response")
        if payload.get("code") != 0:
            raise FeishuAPIError(operation, payload.get("code"), str(payload.get("msg") or "unknown error"))
        response.raise_for_status()
        return payload.get("data") or {}

    async def _paged(
        self,
        path: str,
        *,
        operation: str,
        item_key: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query = {"page_size": 100, **(params or {})}
            if page_token:
                query["page_token"] = page_token
            data = await self._request(
                "GET", path, operation=operation, params=query
            )
            items.extend(data.get(item_key) or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuAPIError(operation, "pagination", "has_more=true but page_token is missing")

    async def list_rooms(self) -> list[dict[str, Any]]:
        buildings = await self._paged(
            "/meeting_room/building/list",
            operation="查询会议室建筑",
            item_key="buildings",
        )
        rooms: list[dict[str, Any]] = []
        for building in buildings:
            building_id = str(building.get("building_id") or "")
            if not building_id:
                continue
            building_rooms = await self._paged(
                "/meeting_room/room/list",
                operation="查询会议室",
                item_key="rooms",
                params={"building_id": building_id},
            )
            for room in building_rooms:
                room = dict(room)
                room["building_name"] = str(building.get("name") or "")
                rooms.append(room)
        return rooms

    @staticmethod
    def _normalize_name(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = re.sub(r"[\s()\[\]{}<>（）【】《》、,_—-]+", "", value)
        digits = str.maketrans("零一二三四五六七八九", "0123456789")
        return value.translate(digits)

    @classmethod
    def match_rooms(
        cls, rooms: list[dict[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        needle = cls._normalize_name(query)
        if not needle:
            return []
        exact = [
            room
            for room in rooms
            if cls._normalize_name(str(room.get("name") or "")) == needle
        ]
        if exact:
            return exact
        return [
            room
            for room in rooms
            if needle
            in cls._normalize_name(
                " ".join(
                    str(room.get(key) or "")
                    for key in ("building_name", "floor_name", "name")
                )
            )
        ]

    @staticmethod
    def _room_label(room: dict[str, Any]) -> str:
        location = " ".join(
            value
            for value in (
                str(room.get("building_name") or "").strip(),
                str(room.get("floor_name") or "").strip(),
            )
            if value
        )
        name = str(room.get("name") or "未知会议室")
        return f"{location} {name}".strip()

    async def _resolve_users(
        self, attendee_names: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        users = await self.directory.list_users()
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in attendee_names:
            matches = self.directory.match_users(users, name)
            if not matches:
                return [], {
                    "booked": False,
                    "reason": "attendee_no_match",
                    "attendee": name,
                    "message": f"通讯录中未找到“{name}”",
                }
            if len(matches) > 1:
                return [], {
                    "booked": False,
                    "reason": "attendee_ambiguous",
                    "attendee": name,
                    "matches": [
                        self.directory._display_name(user) for user in matches[:10]
                    ],
                }
            user = matches[0]
            open_id = str(user.get("open_id") or "")
            if open_id and open_id not in seen:
                resolved.append(user)
                seen.add(open_id)
        return resolved, None

    async def _resolve_room(
        self, room_name: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        rooms = self.match_rooms(await self.list_rooms(), room_name)
        if not rooms:
            return None, {
                "reason": "room_no_match",
                "room": room_name,
                "message": f"未找到会议室“{room_name}”",
            }
        if len(rooms) > 1:
            return None, {
                "reason": "room_ambiguous",
                "room": room_name,
                "matches": [self._room_label(item) for item in rooms[:10]],
            }
        room = rooms[0]
        status = room.get("status") or room.get("room_status") or {}
        if isinstance(status, dict) and (
            status.get("status") is False or status.get("schedule_status") is False
        ):
            return None, {
                "reason": "room_disabled",
                "room": self._room_label(room),
            }
        return room, None

    async def _room_busy(
        self, room_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/meeting_room/freebusy/batch_get",
            operation="查询会议室忙闲",
            params={
                "room_ids": room_id,
                "time_min": start.isoformat(timespec="seconds"),
                "time_max": end.isoformat(timespec="seconds"),
            },
        )
        return ((data.get("free_busy") or {}).get(room_id) or [])

    async def _users_busy(
        self, user_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        if not user_ids:
            return {}
        data = await self._request(
            "POST",
            "/calendar/v4/freebusy/batch",
            operation="查询参与人忙闲",
            params={"user_id_type": "open_id"},
            json={
                "time_min": start.isoformat(timespec="seconds"),
                "time_max": end.isoformat(timespec="seconds"),
                "user_ids": user_ids,
                "only_busy": True,
            },
        )
        return {
            str(item.get("user_id")): item.get("freebusy_items") or []
            for item in data.get("freebusy_lists") or []
        }

    @staticmethod
    def _busy_intervals(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Keep conflict output useful without leaking event IDs or organizer identities."""
        return [
            {
                "start_time": str(item.get("start_time") or ""),
                "end_time": str(item.get("end_time") or ""),
            }
            for item in items
        ]

    async def _primary_calendar_id(self) -> str:
        calendars = await self._paged(
            "/calendar/v4/calendars",
            operation="查询应用日历",
            item_key="calendar_list",
        )
        for calendar in calendars:
            if calendar.get("type") == "primary" and calendar.get("role") in {"owner", "writer"}:
                return str(calendar["calendar_id"])
        for calendar in calendars:
            if calendar.get("role") in {"owner", "writer"}:
                return str(calendar["calendar_id"])
        raise FeishuAPIError("查询应用日历", "no_writable_calendar", "应用没有可写日历")

    async def _delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._request(
            "DELETE",
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            operation="回滚日程",
        )

    async def book_meeting(
        self,
        *,
        requester_open_id: str,
        source_message_id: str,
        attendee_names: list[str],
        summary: str,
        start_time: str,
        room_name: str = "",
        duration_minutes: int = 30,
        timezone_name: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        requester_open_id = requester_open_id.strip()
        summary = summary.strip()
        room_name = room_name.strip()
        attendee_names = [name.strip() for name in attendee_names if name.strip()]
        if not requester_open_id:
            raise PermissionError("requester identity is required")
        if not summary:
            raise ValueError("summary must not be empty")
        if duration_minutes < 1 or duration_minutes > 1440:
            raise ValueError("duration_minutes must be between 1 and 1440")
        start = datetime.fromisoformat(start_time)
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start_time must include a timezone offset")
        end = start + timedelta(minutes=duration_minutes)

        attendees, resolution_error = await self._resolve_users(attendee_names)
        if resolution_error:
            return resolution_error

        room: dict[str, Any] | None = None
        if room_name:
            room, room_error = await self._resolve_room(room_name)
            if room_error:
                return {"booked": False, **room_error}

        participant_by_id = {
            str(user["open_id"]): self.directory._display_name(user)
            for user in attendees
            if user.get("open_id")
        }
        participant_by_id.setdefault(requester_open_id, "发起人")
        user_busy = await self._users_busy(list(participant_by_id), start, end)
        conflicts = [
            {
                "type": "user",
                "name": participant_by_id[user_id],
                "busy": self._busy_intervals(busy),
            }
            for user_id, busy in user_busy.items()
            if busy
        ]
        room_id = str(room.get("room_id") or "") if room else ""
        room_busy = await self._room_busy(room_id, start, end) if room_id else []
        if room and room_busy:
            conflicts.append(
                {
                    "type": "room",
                    "name": self._room_label(room),
                    "busy": self._busy_intervals(room_busy),
                }
            )
        if conflicts:
            return {
                "booked": False,
                "reason": "time_conflict",
                "start_time": start.isoformat(timespec="seconds"),
                "end_time": end.isoformat(timespec="seconds"),
                "conflicts": conflicts,
            }

        calendar_id = await self._primary_calendar_id()
        idempotency_key = source_message_id.strip() or None
        params: dict[str, Any] = {"user_id_type": "open_id"}
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        created = await self._request(
            "POST",
            f"/calendar/v4/calendars/{calendar_id}/events",
            operation="创建日程",
            params=params,
            json={
                "summary": summary,
                "description": "由智能助手根据用户指令创建",
                "start_time": {
                    "timestamp": str(int(start.timestamp())),
                    "timezone": timezone_name,
                },
                "end_time": {
                    "timestamp": str(int(end.timestamp())),
                    "timezone": timezone_name,
                },
                "free_busy_status": "busy",
                "visibility": "default",
                "attendee_ability": "can_see_others",
            },
        )
        event = created.get("event") or {}
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise FeishuAPIError("创建日程", "missing_event_id", "响应没有 event_id")

        user_ids = list(participant_by_id)
        attendee_payload = [
            {"type": "user", "user_id": user_id} for user_id in user_ids
        ]
        if room_id:
            attendee_payload.append({"type": "resource", "room_id": room_id})
        try:
            added = await self._request(
                "POST",
                f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
                operation="添加参与人和会议室",
                params={"user_id_type": "open_id"},
                json={"attendees": attendee_payload, "need_notification": True},
            )
            added_attendees = added.get("attendees") or []
            booked_room = (
                next(
                    (
                        item
                        for item in added_attendees
                        if item.get("type") == "resource"
                        and item.get("room_id") == room_id
                    ),
                    None,
                )
                if room_id
                else None
            )
            if room_id and (
                booked_room is None or booked_room.get("rsvp_status") == "decline"
            ):
                await self._delete_event(calendar_id, event_id)
                return {
                    "booked": False,
                    "reason": "room_declined",
                    "room": self._room_label(room),
                }
        except Exception:
            try:
                await self._delete_event(calendar_id, event_id)
            except Exception:
                pass
            raise

        return {
            "booked": True,
            "summary": summary,
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "duration_minutes": duration_minutes,
            "room": self._room_label(room) if room else None,
            "attendees": [self.directory._display_name(user) for user in attendees],
            "room_status": booked_room.get("rsvp_status") if booked_room else None,
            "event_id": event_id,
            "app_link": event.get("app_link"),
        }

    async def add_room_to_meeting(
        self,
        *,
        requester_open_id: str,
        event_id: str,
        room_name: str,
    ) -> dict[str, Any]:
        """Add a physical room to an app-created meeting the requester attends."""

        requester_open_id = requester_open_id.strip()
        event_id = event_id.strip()
        room_name = room_name.strip()
        if not requester_open_id:
            raise PermissionError("requester identity is required")
        if not room_name:
            raise ValueError("room_name must not be empty")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", event_id) is None:
            raise ValueError("invalid event_id")

        calendar_id = await self._primary_calendar_id()
        event_data = await self._request(
            "GET",
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            operation="查询待修改日程",
            params={"user_id_type": "open_id"},
        )
        event = event_data.get("event") or {}
        if not event:
            raise FeishuAPIError("查询待修改日程", "missing_event", "响应没有 event")

        existing_attendees = await self._paged(
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
            operation="查询日程参与人",
            item_key="items",
            params={"user_id_type": "open_id"},
        )
        requester_is_attendee = any(
            item.get("type") == "user"
            and str(item.get("user_id") or "") == requester_open_id
            for item in existing_attendees
        )
        if not requester_is_attendee:
            raise PermissionError("only an attendee may add a room to this meeting")

        room, room_error = await self._resolve_room(room_name)
        if room_error:
            return {"updated": False, **room_error}
        assert room is not None
        room_id = str(room.get("room_id") or "")
        if any(
            item.get("type") == "resource"
            and str(item.get("room_id") or "") == room_id
            for item in existing_attendees
        ):
            return {
                "updated": False,
                "idempotent": True,
                "reason": "room_already_added",
                "event_id": event_id,
                "room": self._room_label(room),
            }

        start_info = event.get("start_time") or {}
        end_info = event.get("end_time") or {}
        try:
            timezone_name = str(
                start_info.get("timezone") or end_info.get("timezone") or "Asia/Shanghai"
            )
            event_timezone = ZoneInfo(timezone_name)
            start = datetime.fromtimestamp(
                int(start_info["timestamp"]), tz=event_timezone
            )
            end = datetime.fromtimestamp(int(end_info["timestamp"]), tz=event_timezone)
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise FeishuAPIError(
                "查询待修改日程", "invalid_event_time", "日程时间无效"
            ) from exc

        room_busy = await self._room_busy(room_id, start, end)
        if room_busy:
            return {
                "updated": False,
                "reason": "time_conflict",
                "start_time": start.isoformat(timespec="seconds"),
                "end_time": end.isoformat(timespec="seconds"),
                "conflicts": [
                    {
                        "type": "room",
                        "name": self._room_label(room),
                        "busy": self._busy_intervals(room_busy),
                    }
                ],
            }

        added = await self._request(
            "POST",
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
            operation="追加会议室",
            params={"user_id_type": "open_id"},
            json={
                "attendees": [{"type": "resource", "room_id": room_id}],
                "need_notification": True,
            },
        )
        booked_room = next(
            (
                item
                for item in added.get("attendees") or []
                if item.get("type") == "resource"
                and str(item.get("room_id") or "") == room_id
            ),
            None,
        )
        if booked_room is None or booked_room.get("rsvp_status") == "decline":
            attendee_id = str((booked_room or {}).get("attendee_id") or "")
            if attendee_id:
                try:
                    await self._request(
                        "POST",
                        f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees/batch_delete",
                        operation="清理未成功预订的会议室",
                        params={"user_id_type": "open_id"},
                        json={"attendee_ids": [attendee_id], "need_notification": False},
                    )
                except Exception:
                    pass
            return {
                "updated": False,
                "reason": "room_declined",
                "room": self._room_label(room),
            }

        return {
            "updated": True,
            "summary": str(event.get("summary") or ""),
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "room": self._room_label(room),
            "room_status": booked_room.get("rsvp_status"),
            "event_id": event_id,
            "app_link": event.get("app_link"),
        }
