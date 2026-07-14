from __future__ import annotations

import json
from pathlib import Path

import pytest

from reminder_mcp.patch_nanobot import main as patch_nanobot

patch_nanobot()

from nanobot.cron import service as cron_service_module
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def _add_bounded_job(service: CronService, *, until_ms: int):
    return service.add_job(
        name="bounded",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="test",
        session_key="feishu:ou_test",
        origin_channel="feishu",
        origin_chat_id="ou_test",
        origin_metadata={"_cron_until_ms": until_ms},
    )


@pytest.mark.asyncio
async def test_bounded_job_never_runs_at_or_after_end_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now_ms = 1_000_000
    calls: list[str] = []

    async def on_job(job):
        calls.append(job.id)

    monkeypatch.setattr(cron_service_module, "_now_ms", lambda: now_ms)
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    service._running = True
    service._store = cron_service_module.CronStore()
    job = _add_bounded_job(service, until_ms=now_ms + 60_000)
    assert job.enabled is False
    assert job.state.next_run_at_ms is None
    assert calls == []


@pytest.mark.asyncio
async def test_expired_bounded_job_is_removed_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now_ms = 1_000_000
    calls: list[str] = []

    async def on_job(job):
        calls.append(job.id)

    monkeypatch.setattr(cron_service_module, "_now_ms", lambda: now_ms)
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    service._running = True
    service._store = cron_service_module.CronStore()
    job = _add_bounded_job(service, until_ms=now_ms + 120_000)
    assert job.enabled is True

    now_ms += 120_000
    await service._on_timer()

    assert calls == []
    assert service.get_job(job.id) is None


def test_replaying_delete_for_missing_job_is_idempotent(tmp_path: Path):
    store_path = tmp_path / "cron" / "jobs.json"
    action_path = store_path.parent / "action.jsonl"
    action_path.parent.mkdir(parents=True)
    action_path.write_text(
        json.dumps({"action": "del", "params": {"job_id": "already-gone"}}) + "\n",
        encoding="utf-8",
    )
    service = CronService(store_path)
    service._running = True
    service._store = cron_service_module.CronStore()

    service._merge_action()

    assert service._store.jobs == []
    assert action_path.read_text(encoding="utf-8") == ""
