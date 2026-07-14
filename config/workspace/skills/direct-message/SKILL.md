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
