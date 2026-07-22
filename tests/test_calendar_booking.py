import json

import httpx
import pytest

from reminder_mcp.calendar_booking import FeishuCalendarBooker


def _base_response(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    if path.endswith("/auth/v3/tenant_access_token/internal"):
        return httpx.Response(
            200,
            json={"code": 0, "tenant_access_token": "token", "expire": 7200},
        )
    if path.endswith("/contact/v3/departments/0/children"):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [{"department_id": "engineering"}],
                },
            },
        )
    if path.endswith("/contact/v3/users/find_by_department"):
        items = []
        if request.url.params["department_id"] == "engineering":
            items = [{"open_id": "ou_dazhi", "name": "大只"}]
        return httpx.Response(
            200,
            json={"code": 0, "data": {"has_more": False, "items": items}},
        )
    if path.endswith("/meeting_room/building/list"):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "has_more": False,
                    "buildings": [{"building_id": "omb_b2", "name": "B2"}],
                },
            },
        )
    if path.endswith("/meeting_room/room/list"):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "has_more": False,
                    "rooms": [
                        {
                            "room_id": "omm_one",
                            "name": "会议室（一）",
                            "floor_name": "901",
                            "capacity": 30,
                            "room_status": {"status": True},
                        }
                    ],
                },
            },
        )
    return None


@pytest.mark.asyncio
async def test_books_available_room_and_invites_requester(monkeypatch):
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        common = _base_response(request)
        if common is not None:
            return common
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        requests.append((request.method, path, payload))
        if path.endswith("/calendar/v4/freebusy/batch"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "freebusy_lists": [
                            {"user_id": user_id, "freebusy_items": []}
                            for user_id in payload["user_ids"]
                        ]
                    },
                },
            )
        if path.endswith("/meeting_room/freebusy/batch_get"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"free_busy": {"omm_one": []}}},
            )
        if path.endswith("/calendar/v4/calendars"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "calendar_list": [
                            {
                                "calendar_id": "cal_primary",
                                "type": "primary",
                                "role": "owner",
                            }
                        ],
                    },
                },
            )
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "event": {
                            "event_id": "evt_1",
                            "app_link": "https://calendar.feishu.cn/event/evt_1",
                        }
                    },
                },
            )
        if path.endswith("/events/evt_1/attendees"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "attendees": [
                            {
                                "type": "resource",
                                "room_id": "omm_one",
                                "rsvp_status": "accept",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    booker = FeishuCalendarBooker(client)

    result = await booker.book_meeting(
        requester_open_id="ou_requester",
        source_message_id="om_source",
        attendee_names=["大只"],
        room_name="会议室一",
        summary="与大只的会议",
        start_time="2026-07-23T14:00:00+08:00",
    )
    await client.aclose()

    assert result["booked"] is True
    assert result["room"] == "B2 901 会议室（一）"
    assert result["end_time"] == "2026-07-23T14:30:00+08:00"
    attendee_request = next(
        payload for method, path, payload in requests if path.endswith("/attendees")
    )
    assert attendee_request["attendees"] == [
        {"type": "user", "user_id": "ou_dazhi"},
        {"type": "user", "user_id": "ou_requester"},
        {"type": "resource", "room_id": "omm_one"},
    ]
    assert attendee_request["need_notification"] is True


@pytest.mark.asyncio
async def test_conflict_prevents_event_creation(monkeypatch):
    created = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        common = _base_response(request)
        if common is not None:
            return common
        path = request.url.path
        if path.endswith("/calendar/v4/freebusy/batch"):
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "freebusy_lists": [
                            {
                                "user_id": user_id,
                                "freebusy_items": (
                                    [
                                        {
                                            "start_time": "2026-07-23T14:10:00+08:00",
                                            "end_time": "2026-07-23T15:00:00+08:00",
                                        }
                                    ]
                                    if user_id == "ou_dazhi"
                                    else []
                                ),
                            }
                            for user_id in payload["user_ids"]
                        ]
                    },
                },
            )
        if path.endswith("/meeting_room/freebusy/batch_get"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"free_busy": {"omm_one": []}}},
            )
        if path.endswith("/events"):
            created = True
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    booker = FeishuCalendarBooker(client)

    result = await booker.book_meeting(
        requester_open_id="ou_requester",
        source_message_id="om_source",
        attendee_names=["大只"],
        room_name="会议室一",
        summary="与大只的会议",
        start_time="2026-07-23T14:00:00+08:00",
    )
    await client.aclose()

    assert result["booked"] is False
    assert result["reason"] == "time_conflict"
    assert result["conflicts"][0]["name"] == "大只"
    assert created is False


def test_room_name_normalization_matches_chinese_parentheses_and_digits():
    rooms = [{"room_id": "omm_one", "name": "会议室（一）"}]

    assert FeishuCalendarBooker.match_rooms(rooms, "会议室一") == rooms
    assert FeishuCalendarBooker.match_rooms(rooms, "会议室1") == rooms
