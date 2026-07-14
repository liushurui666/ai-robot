---
name: message-digest
description: Collect messages across days and prepare confirmed Feishu digests.
---

# Message digest workflow

Use this workflow when a user asks to record information for later or summarize previously
recorded information.

## Request identity

`record_message`, `create_digest_draft`, `list_digest_drafts`, `confirm_digest`, `cancel_digest`,
and `update_record` are native identity-bound tools. They read the current nanobot request context
directly and do not accept identity arguments. Never use similarly named MCP tools or ask the user
for an open_id. If the runtime cannot verify the current sender, the tool will reject the operation.

## Recording

1. Only persist content when the user explicitly says to record, save, collect, or add it to a
   named topic. Normal conversation must never be stored as a digest record.
2. Resolve relative dates in Asia/Shanghai and pass an ISO-8601 timestamp with `+08:00` to
   `record_message`.
3. Prefer the Feishu message id as `source_message_id`; this provides idempotency.
4. Call the identity-bound `record_message`, then reply with the topic and stored record id. If the
   topic is ambiguous, ask before storing.
5. For requests to edit, cancel, archive, or restore a stored message, call `update_record`; do not
   silently remove historical rows.

## Immediate digest

1. Resolve the requested range in Asia/Shanghai. Inclusive end-of-day means `23:59:59+08:00`.
   If the user says "since the last digest" or gives no start, call `suggest_digest_range`.
2. Call `list_digest_records`. Do not rely on chat memory and do not invent missing facts.
3. Call `list_delivery_targets`; users may select only an administrator-approved alias.
4. Write a concise Chinese summary that separates completed work, progress, risks, and actions
   when those categories are supported by the source messages.
5. Call the identity-bound `create_digest_draft`, show its exact content and draft id, then request
   confirmation. Keep the returned confirmation token in the requester's conversation context; do
   not show it in a shared destination group.
6. On an explicit confirmation, call the identity-bound `confirm_digest` with the token. The tool
   reads the current sender for that turn and verifies it matches the requester. Preparation,
   scheduling, or silence is never confirmation.

## Future digest

For a future date, use nanobot's native cron tool to create a one-time task. The scheduled prompt
must name the topic, absolute Asia/Shanghai range, target alias, requester and the requirement to:

1. list source records;
2. create a digest draft;
3. send the preview back to the requester for confirmation;
4. never call `confirm_digest` automatically.

The final destination receives nothing until the requester confirms the pending draft.

For status questions, use the identity-bound `list_digest_drafts`. A failed pending draft may be
confirmed again; sent drafts are idempotent and must not be re-sent.
