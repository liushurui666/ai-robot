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


@pytest.mark.asyncio
async def test_books_meeting_without_room_and_skips_room_apis(monkeypatch):
    attendee_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attendee_payload
        path = request.url.path
        if "/meeting_room/" in path:
            raise AssertionError(f"room API must not be called: {request.url}")
        common = _base_response(request)
        if common is not None:
            return common
        payload = json.loads(request.content) if request.content else {}
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
                json={"code": 0, "data": {"event": {"event_id": "evt_no_room"}}},
            )
        if path.endswith("/events/evt_no_room/attendees"):
            attendee_payload = payload
            return httpx.Response(
                200,
                json={"code": 0, "data": {"attendees": []}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    booker = FeishuCalendarBooker(client)

    result = await booker.book_meeting(
        requester_open_id="ou_requester",
        source_message_id="om_no_room",
        attendee_names=["大只"],
        summary="数据产品工作规划对齐",
        start_time="2026-07-24T16:30:00+08:00",
    )
    await client.aclose()

    assert result["booked"] is True
    assert result["room"] is None
    assert attendee_payload["attendees"] == [
        {"type": "user", "user_id": "ou_dazhi"},
        {"type": "user", "user_id": "ou_requester"},
    ]


@pytest.mark.asyncio
async def test_adds_room_to_existing_meeting(monkeypatch):
    added_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal added_payload
        path = request.url.path
        common = _base_response(request)
        if common is not None:
            return common
        if request.method == "GET" and path.endswith("/calendar/v4/calendars"):
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
        if request.method == "GET" and path.endswith("/events/evt_existing"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "event": {
                            "event_id": "evt_existing",
                            "summary": "数据产品工作规划对齐",
                            "start_time": {
                                "timestamp": "1784883600",
                                "timezone": "Asia/Shanghai",
                            },
                            "end_time": {
                                "timestamp": "1784885400",
                                "timezone": "Asia/Shanghai",
                            },
                            "app_link": "https://calendar.feishu.cn/event/evt_existing",
                        }
                    },
                },
            )
        if request.method == "GET" and path.endswith(
            "/events/evt_existing/attendees"
        ):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "type": "user",
                                "user_id": "ou_requester",
                                "display_name": "发起人",
                            },
                            {
                                "type": "user",
                                "user_id": "ou_dazhi",
                                "display_name": "大只",
                            },
                        ],
                    },
                },
            )
        if path.endswith("/meeting_room/freebusy/batch_get"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"free_busy": {"omm_one": []}}},
            )
        if request.method == "POST" and path.endswith(
            "/events/evt_existing/attendees"
        ):
            added_payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "attendees": [
                            {
                                "type": "resource",
                                "attendee_id": "resource_one",
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

    result = await booker.add_room_to_meeting(
        requester_open_id="ou_requester",
        event_id="evt_existing",
        room_name="会议室一",
    )
    await client.aclose()

    assert result["updated"] is True
    assert result["summary"] == "数据产品工作规划对齐"
    assert result["start_time"] == "2026-07-24T17:00:00+08:00"
    assert result["room"] == "B2 901 会议室（一）"
    assert added_payload == {
        "attendees": [{"type": "resource", "room_id": "omm_one"}],
        "need_notification": True,
    }


@pytest.mark.asyncio
async def test_non_attendee_cannot_add_room_to_meeting(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/meeting_room/" in path:
            raise AssertionError("room lookup must happen after authorization")
        common = _base_response(request)
        if common is not None:
            return common
        if request.method == "GET" and path.endswith("/calendar/v4/calendars"):
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
        if request.method == "GET" and path.endswith("/events/evt_existing"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "event": {
                            "event_id": "evt_existing",
                            "summary": "其他人的会议",
                        }
                    },
                },
            )
        if request.method == "GET" and path.endswith(
            "/events/evt_existing/attendees"
        ):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [{"type": "user", "user_id": "ou_other"}],
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    booker = FeishuCalendarBooker(client)

    with pytest.raises(PermissionError, match="attendee"):
        await booker.add_room_to_meeting(
            requester_open_id="ou_requester",
            event_id="evt_existing",
            room_name="会议室一",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_adding_same_room_again_is_idempotent(monkeypatch):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                AssertionError(f"unexpected network request: {request.url}")
            )
        )
    )
    booker = FeishuCalendarBooker(client)

    async def primary_calendar_id():
        return "cal_primary"

    async def get_event(*args, **kwargs):
        return {"event": {"event_id": "evt_existing", "summary": "项目会议"}}

    async def list_attendees(*args, **kwargs):
        return [
            {"type": "user", "user_id": "ou_requester"},
            {"type": "resource", "room_id": "omm_one"},
        ]

    async def resolve_room(room_name):
        return (
            {
                "room_id": "omm_one",
                "name": "会议室（一）",
                "building_name": "B2",
                "floor_name": "901",
            },
            None,
        )

    async def room_busy(*args, **kwargs):
        raise AssertionError("an already-added room must not be checked or added again")

    monkeypatch.setattr(booker, "_primary_calendar_id", primary_calendar_id)
    monkeypatch.setattr(booker, "_request", get_event)
    monkeypatch.setattr(booker, "_paged", list_attendees)
    monkeypatch.setattr(booker, "_resolve_room", resolve_room)
    monkeypatch.setattr(booker, "_room_busy", room_busy)

    result = await booker.add_room_to_meeting(
        requester_open_id="ou_requester",
        event_id="evt_existing",
        room_name="会议室一",
    )
    await client.aclose()

    assert result == {
        "updated": False,
        "idempotent": True,
        "reason": "room_already_added",
        "event_id": "evt_existing",
        "room": "B2 901 会议室（一）",
    }


def test_room_name_normalization_matches_chinese_parentheses_and_digits():
    rooms = [{"room_id": "omm_one", "name": "会议室（一）"}]

    assert FeishuCalendarBooker.match_rooms(rooms, "会议室一") == rooms
    assert FeishuCalendarBooker.match_rooms(rooms, "会议室1") == rooms
