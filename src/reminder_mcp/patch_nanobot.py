from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path


FEISHU_PATCHES = (
    (
        "verified sender identity",
        """                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,""",
        """                metadata={
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "chat_type": chat_type,""",
    ),
    (
        "processing card configuration",
        '''    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"''',
        '''    streaming: bool = True
    processing_card: bool = False
    processing_text: str = "⏳ 正在处理…"
    domain: Literal["feishu", "lark"] = "feishu"''',
    ),
    (
        "processing card state",
        """        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id

    # ------------------------------------------------------------------""",
        """        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id
        self._processing_cards: OrderedDict[str, str] = OrderedDict()

    # ------------------------------------------------------------------""",
    ),
    (
        "processing card API helpers",
        """    def _create_streaming_card_sync(
""",
        """    @staticmethod
    def _processing_card_content(content: str, *, processing: bool) -> str:
        text = (content or "").strip()
        encoded = text.encode("utf-8")
        if len(encoded) > 20_000:
            text = encoded[:19_940].decode("utf-8", errors="ignore") + "\\n\\n…内容过长，已截断"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue" if processing else "green",
                "title": {
                    "tag": "plain_text",
                    "content": "正在处理" if processing else "处理完成",
                },
            },
            "elements": [{"tag": "markdown", "content": text}],
        }
        return json.dumps(card, ensure_ascii=False)

    def _send_processing_card_sync(
        self, receive_id_type: str, chat_id: str
    ) -> str | None:
        return self._send_message_sync(
            receive_id_type,
            chat_id,
            "interactive",
            self._processing_card_content(
                self.config.processing_text,
                processing=True,
            ),
        )

    def _update_processing_card_sync(self, message_id: str, content: str) -> bool:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(self._processing_card_content(content, processing=False))
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.patch(request)
            if not response.success():
                self.logger.warning(
                    "Failed to update processing card {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return False
            self.logger.info("Processing card {} updated with final response", message_id)
            return True
        except Exception:
            self.logger.exception("Error updating processing card {}", message_id)
            return False

    def _create_streaming_card_sync(
""",
    ),
    (
        "processing card final update",
        """            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            # Handle tool hint messages.""",
        """            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()
            updated_processing_card = False

            # Progress and tool hints must not consume the pending card. Only the
            # final answer replaces the placeholder, keeping the turn in one card.
            source_message_id = msg.metadata.get("message_id")
            if (
                self.config.processing_card
                and source_message_id
                and msg.content
                and msg.content.strip()
                and not msg.metadata.get("_progress")
                and not msg.metadata.get("_stream_delta")
                and not msg.metadata.get("_stream_end")
            ):
                processing_message_id = self._processing_cards.pop(
                    str(source_message_id), None
                )
                if processing_message_id:
                    updated_processing_card = await loop.run_in_executor(
                        None,
                        self._update_processing_card_sync,
                        processing_message_id,
                        msg.content,
                    )
                    if updated_processing_card and not msg.media:
                        return

            # Handle tool hint messages.""",
    ),
    (
        "avoid duplicate final content",
        """            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)""",
        """            if not updated_processing_card and msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)""",
    ),
    (
        "optional reaction",
        """            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content""",
        """            # Add an optional reaction. Processing-card mode can leave
            # reactEmoji empty and avoid requiring reaction-write permissions.
            if self.config.react_emoji:
                task = asyncio.create_task(
                    self._add_reaction(message_id, self.config.react_emoji)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._on_background_task_done)
                task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content""",
    ),
    (
        "create processing card before dispatch",
        """            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(""",
        """            # Forward to message bus. In non-streaming mode, publish a
            # placeholder first and remember its message id for the final update.
            reply_to = chat_id if chat_type == "group" else sender_id
            if self.config.processing_card and not self.config.streaming:
                loop = asyncio.get_running_loop()
                receive_id_type = "chat_id" if reply_to.startswith("oc_") else "open_id"
                processing_message_id = await loop.run_in_executor(
                    None,
                    self._send_processing_card_sync,
                    receive_id_type,
                    reply_to,
                )
                if processing_message_id:
                    self._processing_cards[message_id] = processing_message_id
                    while len(self._processing_cards) > 500:
                        self._processing_cards.popitem(last=False)

            await self._handle_message(""",
    ),
    (
        "collision-safe Feishu media filenames",
        '''        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename''',
        '''        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            resource_key = str(
                content_json.get("image_key") or content_json.get("file_key") or "media"
            )
            message_part = str(message_id or uuid.uuid4().hex)[-12:]
            resource_part = safe_filename(resource_key[:12]) or "media"
            filename = f"{message_part}-{resource_part}-{filename}"
            file_path = media_dir / filename''',
    ),
)

AGENT_LOOP_PATCHES = (
    (
        "error response routing metadata",
        """                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))""",
        """                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                        metadata=dict(msg.metadata or {}),
                    ))""",
    ),
    (
        "deterministic image turns",
        '''                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,''',
        '''                max_tool_result_chars=self.max_tool_result_chars,
                # Vision/OCR requests should be deterministic. A high sampling
                # temperature can turn an unreadable region into an invented
                # description even when the image itself was delivered.
                temperature=(
                    0.0
                    if any(
                        isinstance(message.get("content"), list)
                        and any(
                            isinstance(block, dict) and block.get("type") == "image_url"
                            for block in message["content"]
                        )
                        for message in initial_messages
                    )
                    else None
                ),
                hook=hook,''',
    ),
)

CRON_TOOL_PATCHES = (
    (
        "bounded recurrence schema",
        '''    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    job_id=StringSchema''',
        '''    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    until=StringSchema(
        "Exclusive ISO end datetime for a bounded recurring task. Use with every_seconds "
        "or cron_expr when the user says 'until/by/before a time'. Naive values use the "
        "schedule timezone. Never encode an end time as a cron hour range."
    ),
    job_id=StringSchema''',
    ),
    (
        "bounded recurrence parameter guidance",
        '''        "Action-specific parameters: add requires a non-empty message plus one schedule "
        "(every_seconds, cron_expr, or at); remove requires job_id; list only needs action. "''',
        '''        "Action-specific parameters: add requires a non-empty message plus one schedule "
        "(every_seconds, cron_expr, or at); recurring requests with an end time must also pass "
        "until; remove requires job_id; list only needs action. "''',
    ),
    (
        "bounded recurrence tool description",
        '''            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."''',
        '''            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            "For any recurring request with an end time, pass the exclusive `until` datetime; "
            "never approximate an end time with a cron hour range. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."''',
    ),
    (
        "bounded recurrence execute argument",
        '''        at: str | None = None,
        job_id: str | None = None,''',
        '''        at: str | None = None,
        until: str | None = None,
        job_id: str | None = None,''',
    ),
    (
        "bounded recurrence execute forwarding",
        '''            return self._add_job(name, message, every_seconds, cron_expr, tz, at)''',
        '''            return self._add_job(name, message, every_seconds, cron_expr, tz, at, until)''',
    ),
    (
        "bounded recurrence add signature",
        '''        tz: str | None,
        at: str | None,
    ) -> str:''',
        '''        tz: str | None,
        at: str | None,
        until: str | None,
    ) -> str:''',
    ),
    (
        "bounded recurrence validation",
        '''        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule''',
        '''        if tz:
            if err := self._validate_timezone(tz):
                return err

        until_ms: int | None = None
        if until:
            if at or not (every_seconds or cron_expr):
                return "Error: until can only be used with every_seconds or cron_expr"
            from zoneinfo import ZoneInfo

            try:
                until_dt = datetime.fromisoformat(until)
            except ValueError:
                return (
                    f"Error: invalid ISO datetime format '{until}'. "
                    "Expected format: YYYY-MM-DDTHH:MM:SS"
                )
            if until_dt.tzinfo is None:
                until_tz = tz or self._default_timezone
                if err := self._validate_timezone(until_tz):
                    return err
                until_dt = until_dt.replace(tzinfo=ZoneInfo(until_tz))
            until_ms = int(until_dt.timestamp() * 1000)
            if until_ms <= int(datetime.now().timestamp() * 1000):
                return "Error: until must be in the future"

        # Build schedule''',
    ),
    (
        "bounded recurrence metadata",
        '''        job = self._cron.add_job(
            name=name or message[:30],''',
        '''        origin_metadata = dict(self._origin_metadata.get() or {})
        if until_ms is not None:
            origin_metadata["_cron_until_ms"] = until_ms

        job = self._cron.add_job(
            name=name or message[:30],''',
    ),
    (
        "bounded recurrence metadata forwarding",
        '''            origin_metadata=dict(self._origin_metadata.get() or {}),
        )
        return f"Created job '{job.name}' (id: {job.id})"''',
        '''            origin_metadata=origin_metadata,
        )
        if until_ms is not None and not job.enabled:
            self._cron.remove_job(job.id)
            return "Error: the recurring schedule has no execution before until"
        if until_ms is not None:
            return (
                f"Created job '{job.name}' (id: {job.id}), ending before "
                f"{self._format_timestamp(until_ms, tz or self._default_timezone)}"
            )
        return f"Created job '{job.name}' (id: {job.id})"''',
    ),
)

CRON_SERVICE_PATCHES = (
    (
        "idempotent queued deletion",
        '''            if job_id := params.get("job_id"):
                jobs_map.pop(job_id)''',
        '''            if job_id := params.get("job_id"):
                jobs_map.pop(job_id, None)''',
    ),
    (
        "bounded recurrence metadata helper",
        '''def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run''',
        '''def _now_ms() -> int:
    return int(time.time() * 1000)


def _job_until_ms(job: CronJob) -> int | None:
    """Return the exclusive end boundary for a bounded recurring job."""
    raw = (job.payload.origin_metadata or {}).get("_cron_until_ms")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _compute_next_run''',
    ),
    (
        "bounded recurrence restart handling",
        '''            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)''',
        '''            if job.enabled:
                next_run = _compute_next_run(job.schedule, now)
                until_ms = _job_until_ms(job)
                if until_ms is not None and (next_run is None or next_run >= until_ms):
                    job.enabled = False
                    job.state.next_run_at_ms = None
                else:
                    job.state.next_run_at_ms = next_run''',
    ),
    (
        "bounded recurrence due filtering",
        '''            now = _now_ms()
            due_jobs = [
                j for j in self._store.jobs
                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
            ]''',
        '''            now = _now_ms()
            expired_ids = {
                j.id
                for j in self._store.jobs
                if j.enabled
                and (until_ms := _job_until_ms(j)) is not None
                and now >= until_ms
            }
            if expired_ids:
                self._store.jobs = [j for j in self._store.jobs if j.id not in expired_ids]
                for job_id in expired_ids:
                    logger.info("Cron: removed bounded job {} at its end boundary", job_id)
            due_jobs = [
                j for j in self._store.jobs
                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
            ]''',
    ),
    (
        "bounded recurrence next run handling",
        '''        else:
            # Compute next run
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())''',
        '''        else:
            # Compute the next run, respecting an exclusive end boundary.
            next_run = _compute_next_run(job.schedule, _now_ms())
            until_ms = _job_until_ms(job)
            if until_ms is not None and (next_run is None or next_run >= until_ms):
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
                logger.info("Cron: completed bounded job '{}' ({})", job.name, job.id)
            else:
                job.state.next_run_at_ms = next_run''',
    ),
    (
        "bounded recurrence add handling",
        '''        _normalize_agent_turn_job(job)
        self._enforce_agent_binding(job)
        if self._running:''',
        '''        _normalize_agent_turn_job(job)
        self._enforce_agent_binding(job)
        until_ms = _job_until_ms(job)
        if (
            job.enabled
            and until_ms is not None
            and (job.state.next_run_at_ms is None or job.state.next_run_at_ms >= until_ms)
        ):
            job.enabled = False
            job.state.next_run_at_ms = None
        if self._running:''',
    ),
)


def _module_path(module_name: str) -> Path:
    spec = find_spec(module_name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"{module_name} is not installed")
    return Path(spec.origin)


def _apply_patches(path: Path, patches: tuple[tuple[str, str, str], ...]) -> None:
    source = path.read_text(encoding="utf-8")
    changed: list[str] = []
    for name, old, new in patches:
        if new in source:
            continue
        if source.count(old) != 1:
            raise RuntimeError(
                f"nanobot source no longer matches the pinned 0.2.2 {name} patch"
            )
        source = source.replace(old, new)
        changed.append(name)
    if changed:
        path.write_text(source, encoding="utf-8")
        print(f"nanobot patches applied to {path}: {', '.join(changed)}")
    else:
        print(f"nanobot patches already applied: {path}")


def main() -> None:
    _apply_patches(_module_path("nanobot.channels.feishu"), FEISHU_PATCHES)
    _apply_patches(_module_path("nanobot.agent.loop"), AGENT_LOOP_PATCHES)
    _apply_patches(_module_path("nanobot.agent.tools.cron"), CRON_TOOL_PATCHES)
    _apply_patches(_module_path("nanobot.cron.service"), CRON_SERVICE_PATCHES)


if __name__ == "__main__":
    main()
