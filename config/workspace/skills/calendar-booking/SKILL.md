---
name: calendar-booking
description: Book a Feishu calendar meeting with colleagues and a physical meeting room.
---

# Feishu calendar booking workflow

Use this workflow only when the current user explicitly asks to book, reserve, or schedule a
meeting. A request to merely discuss possible times must not create an event.

1. Convert the requested start time to an ISO-8601 timestamp with an explicit timezone offset.
   Use `Asia/Shanghai` for company-local times unless the user specifies another timezone.
2. If the user omits the duration, use 30 minutes. Do not ask a follow-up only for duration.
3. If the user omits a title, derive a short factual title such as `与大只的会议` from the
   named attendees. Do not invent an agenda.
4. Call `book_feishu_meeting` once with all named attendees, the physical room, title, start time,
   and duration. The tool automatically adds the verified requester and checks everyone and the
   room for conflicts.
5. When `booked=true`, report the exact title, date, start/end time, room, and invited colleagues.
6. For `time_conflict`, report each conflicting person or room and its busy interval. Do not claim
   that anything was booked.
7. For `attendee_no_match`, `attendee_ambiguous`, `room_no_match`, or `room_ambiguous`, use the
   returned candidates to ask only the necessary clarification.
8. Never ask for an open_id, room_id, calendar_id, or event_id. These are resolved by the tool.
