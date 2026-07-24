---
name: calendar-booking
description: Book a Feishu calendar meeting with colleagues, with an optional physical room.
---

# Feishu calendar booking workflow

Use this workflow only when the current user explicitly asks to book, reserve, or schedule a
meeting. A request to merely discuss possible times must not create an event.

1. Convert the requested start time to an ISO-8601 timestamp with an explicit timezone offset.
   Use `Asia/Shanghai` for company-local times unless the user specifies another timezone.
2. If the user omits the duration, use 30 minutes. Do not ask a follow-up only for duration.
3. If the user omits a title, derive a short factual title such as `与大只的会议` from the
   named attendees. Do not invent an agenda.
4. A physical meeting room is optional. If the user does not name one, or says no room is needed,
   pass an empty `room` value and continue immediately. Never ask the user to choose a room merely
   because they asked to schedule a meeting.
5. Call `book_feishu_meeting` once with all named attendees, the optional physical room, title,
   start time, and duration. The tool automatically adds the verified requester and checks all
   relevant people and, when present, the room for conflicts.
6. When `booked=true`, report the exact title, date, start/end time, invited colleagues, and the
   room only when one was booked.
7. For `time_conflict`, report each conflicting person or room and its busy interval. Do not claim
   that anything was booked.
8. For `attendee_no_match`, `attendee_ambiguous`, `room_no_match`, or `room_ambiguous`, use the
   returned candidates to ask only the necessary clarification.
9. Never ask for an open_id, room_id, calendar_id, or event_id. These are resolved by the tool.
