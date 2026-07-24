---
name: conversation-review
description: Summarize the current user's own recent Feishu conversation and weekly activity.
---

# Private conversation review

Use this workflow when the user asks to extract, recap, or summarize their general activity from
today, this week, or another recent time range. This differs from a named message-digest topic:
general activity comes from the current private conversation, while explicit saved topics use the
structured digest tools.

1. Resolve the requested time range in `Asia/Shanghai` and pass absolute ISO-8601 timestamps with
   `+08:00` to `list_my_recent_conversation`. For “本周”, start at Monday 00:00 and end at the
   current time unless the user asks for the complete calendar week.
2. Always call the tool before claiming that no records exist. Do not inspect
   `memory/history.jsonl`, `MEMORY.md`, or session files with generic file tools.
3. The tool is identity-bound to the current private Feishu session. If it rejects a group chat,
   ask the user to send the request in their private chat with the assistant.
4. Summarize only facts supported by returned entries. Deduplicate compacted summaries against
   exact turns and do not treat the current “please summarize” instruction as an activity.
5. For a weekly review, always use this exact section order and do not repeat headings:

   ```text
   一、本周主要工作业绩
   1. ...
   2. ...

   当前存在问题：
   1. ...

   需要支持：
   1. ...

   二、下周工作主要开展
   1. ...
   2. ...
   ```

   Put confirmed completions, progress, meetings, and coordination outcomes under “本周主要工作业绩”.
   Put unresolved blockers or risks under “当前存在问题”. Put explicit help, decisions, resources,
   or cross-team coordination needed under “需要支持”. Put future plans, scheduled follow-ups, and
   pending actions under “下周工作主要开展”.
6. Keep all four weekly-review blocks even when one has no evidence. In that block write
   “暂无从本次对话记录中提炼出的内容”; never invent an issue, support request, or next-week plan.
7. Keep dates, people, project names, and outcomes precise. Distinguish a requested action from a
   confirmed successful outcome.
8. If `truncated=true`, state that the result covers only the returned portion. If `count=0`, say
   that this private conversation has no retained entries in the requested range; do not claim
   that all company systems or all of the user's work have no records.
