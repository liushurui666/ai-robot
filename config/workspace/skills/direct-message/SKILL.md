---
name: direct-message
description: Send a direct Feishu message to a named company colleague.
---

# Direct Feishu message workflow

Use this workflow when the current user explicitly asks to send a message to a named colleague.

1. If both the recipient name and message content are present, call
   `send_feishu_user_message` immediately.
2. Never ask the user for an open_id, user_id, email address, or phone number. The tool resolves
   company directory names, English names, and nicknames.
3. If the tool returns `sent=true`, report the resolved display name and successful delivery.
4. If the tool returns `ambiguous`, show the returned names and ask which person was intended.
5. If the tool returns `no_match`, ask for a more specific company-directory name.
6. Do not invent missing message content or a missing recipient, and do not claim delivery before
   the tool returns `sent=true`.

## Scheduled direct messages

- Use the native `cron` tool when the user asks to send the message later or repeatedly. The
  scheduled prompt must call `send_feishu_user_message` with the requested recipient and content.
- When a repeated schedule has an end time (for example, "every minute until 16:00"), pass that
  time as the cron tool's exclusive `until` ISO datetime. Never represent the end time by putting
  both the start and end hours in `cron_expr`; a range such as `15-16` runs throughout 16:59.
- State the interpreted interval and exclusive end time in the confirmation response.
