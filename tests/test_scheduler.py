"""Tests for the scheduler service."""

import asyncio
from datetime import timedelta

import pytest
from claudia_channels.scheduler import MISSED_ONE_OFF_GRACE_SECONDS, SchedulerService, format_jobs
from claudia_core import SchedulerError
from claudia_store import SqliteStore


def _scheduler(store: SqliteStore, **kwargs) -> SchedulerService:
    return SchedulerService(store, timezone="Asia/Almaty", tick_seconds=0.01, **kwargs)


class _Runner:
    """Records which profiles were woken and with what text."""

    def __init__(self) -> None:
        self.woken: list[tuple[str, str]] = []

    async def run_scheduled_text(self, profile_name: str, text: str) -> None:
        self.woken.append((profile_name, text))


async def _tick(scheduler: SchedulerService, runner: _Runner | None = None) -> list:
    runner = runner or _Runner()
    fired = scheduler.tick(runner)
    await asyncio.sleep(0)  # let the dispatched tasks run
    return fired


async def test_add_one_off_and_list(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    when = scheduler.now() + timedelta(hours=2)

    job = await scheduler.add(
        profile_name="admin",
        prompt="remind me",
        title="Reminder",
        at=when.strftime("%Y-%m-%d %H:%M"),
    )

    assert job.kind == "once"
    assert job.next_run_at.strftime("%Y-%m-%d %H:%M") == when.strftime("%Y-%m-%d %H:%M")
    assert [j.id for j in scheduler.list_for_profile("admin")] == [job.id]
    assert scheduler.list_for_profile("sister") == []


async def test_add_cron_computes_next_run(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(
        profile_name="admin", prompt="daily", title="Daily", cron="*/5 * * * *"
    )

    assert job.kind == "cron"
    assert job.cron == "*/5 * * * *"
    assert job.next_run_at > scheduler.now()
    assert "cron" in job.schedule_description


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one"),
        ({"at": "2026-01-01 09:00", "cron": "0 9 * * *"}, "exactly one"),
        ({"cron": "not a cron"}, "5 fields"),
        ({"at": "yesterday"}, "Invalid datetime"),
        ({"at": "2000-01-01 09:00"}, "in the past"),
    ],
)
async def test_add_rejects_bad_input(seeded: SqliteStore, kwargs: dict, message: str) -> None:
    scheduler = _scheduler(seeded)
    with pytest.raises(SchedulerError, match=message):
        await scheduler.add(profile_name="admin", prompt="p", title="t", **kwargs)


async def test_empty_prompt_rejected(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    with pytest.raises(SchedulerError):
        await scheduler.add(profile_name="admin", prompt="   ", title="t", cron="0 9 * * *")


async def test_delete_only_own_jobs(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(profile_name="admin", prompt="p", title="t", cron="0 9 * * *")

    assert scheduler.delete("sister", job.id) is False
    assert scheduler.delete("admin", job.id) is True
    assert scheduler.delete("admin", job.id) is False
    assert scheduler.list_for_profile("admin") == []


async def test_jobs_are_isolated_per_profile(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    await scheduler.add(profile_name="admin", prompt="a", title="A", cron="0 9 * * *")
    await scheduler.add(profile_name="sister", prompt="b", title="B", cron="0 9 * * *")

    assert len(scheduler.list_for_profile("admin")) == 1
    assert len(scheduler.list_for_profile("sister")) == 1


async def test_max_jobs_per_profile(seeded: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import claudia_channels.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "MAX_JOBS_PER_PROFILE", 2)
    scheduler = _scheduler(seeded)
    await scheduler.add(profile_name="admin", prompt="a", title="A", cron="0 9 * * *")
    await scheduler.add(profile_name="admin", prompt="b", title="B", cron="0 10 * * *")

    with pytest.raises(SchedulerError, match="limit 2"):
        await scheduler.add(profile_name="admin", prompt="c", title="C", cron="0 11 * * *")


async def test_tick_fires_due_cron_job_and_reschedules(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(profile_name="admin", prompt="p", title="t", cron="*/5 * * * *")
    seeded._conn.execute(
        "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?",
        ((scheduler.now() - timedelta(minutes=1)).isoformat(), job.id),
    )

    fired = await _tick(scheduler)
    assert [j.id for j in fired] == [job.id]

    stored = seeded.get_job("admin", job.id)
    assert stored is not None
    assert stored.next_run_at > scheduler.now()
    assert stored.last_run_at is not None

    # Not due any more.
    assert await _tick(scheduler) == []


async def test_tick_fires_and_removes_one_off_job(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(
        profile_name="admin",
        prompt="p",
        title="t",
        at=(scheduler.now() + timedelta(minutes=1)).isoformat(),
    )
    seeded._conn.execute(
        "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?",
        ((scheduler.now() - timedelta(seconds=5)).isoformat(), job.id),
    )

    fired = await _tick(scheduler)
    assert [j.id for j in fired] == [job.id]
    assert scheduler.list_for_profile("admin") == []


async def test_stale_one_off_job_is_dropped_not_run(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(
        profile_name="admin",
        prompt="p",
        title="t",
        at=(scheduler.now() + timedelta(minutes=1)).isoformat(),
    )
    stale = scheduler.now() - timedelta(seconds=MISSED_ONE_OFF_GRACE_SECONDS + 60)
    seeded._conn.execute(
        "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (stale.isoformat(), job.id)
    )

    assert await _tick(scheduler) == []
    assert scheduler.list_for_profile("admin") == []


async def test_future_jobs_are_not_fired(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    await scheduler.add(profile_name="admin", prompt="p", title="t", cron="0 9 * * *")
    assert await _tick(scheduler) == []


async def test_runner_failure_does_not_break_the_loop(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(profile_name="admin", prompt="p", title="t", cron="*/5 * * * *")
    seeded._conn.execute(
        "UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?",
        ((scheduler.now() - timedelta(minutes=1)).isoformat(), job.id),
    )

    class BoomRunner:
        async def run_scheduled_text(self, profile_name: str, text: str) -> None:
            raise RuntimeError("nope")

    scheduler.tick(BoomRunner())
    await asyncio.sleep(0)
    await scheduler.stop()

    stored = seeded.get_job("admin", job.id)
    assert stored is not None and stored.next_run_at > scheduler.now()


async def test_naive_times_are_interpreted_in_the_scheduler_timezone(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    parsed = scheduler.parse_datetime("2030-01-02 03:04")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(hours=5)  # Asia/Almaty
    assert (parsed.hour, parsed.minute) == (3, 4)


async def test_aware_times_are_converted(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    parsed = scheduler.parse_datetime("2030-01-02T00:00:00+00:00")
    assert (parsed.hour, parsed.minute) == (5, 0)


def test_format_jobs_empty() -> None:
    assert format_jobs([]) == "No scheduled tasks."


async def test_format_jobs_lists_ids_and_prompts(seeded: SqliteStore) -> None:
    scheduler = _scheduler(seeded)
    job = await scheduler.add(
        profile_name="admin", prompt="write report", title="Report", cron="0 9 * * *"
    )
    rendered = format_jobs(scheduler.list_for_profile("admin"))
    assert f"id={job.id}" in rendered
    assert "Report" in rendered
    assert "write report" in rendered


async def test_start_and_stop_tick_loop_fires_due_jobs(seeded: SqliteStore) -> None:
    """End-to-end: the real asyncio tick loop, not a manually driven tick()."""
    scheduler = _scheduler(seeded)
    job = await scheduler.add(
        profile_name="admin",
        prompt="p",
        title="t",
        at=(scheduler.now() + timedelta(seconds=0.05)).isoformat(),
    )
    runner = _Runner()
    scheduler.start(runner)
    try:
        for _ in range(200):
            if runner.woken:
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    assert runner.woken and runner.woken[0][0] == "admin"
    assert job.prompt in runner.woken[0][1]
