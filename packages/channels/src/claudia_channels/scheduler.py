"""Persistent cron/one-off task scheduler.

Jobs are stored through the Repository (claudia_store.SqliteStore), so they survive
restarts. A background loop ticks every few seconds, fires everything that is due and
hands it to a JobRunner (which wakes the profile's agent, same as an inbound message).

Everything is computed in one configured timezone, so what the person says is what the
person gets.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from claudia_core import ScheduledJob, SchedulerError

from claudia_channels.cron import parse_cron
from claudia_channels.port import Repository

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"
DEFAULT_TICK_SECONDS = 20.0
MAX_JOBS_PER_PROFILE = 50

# A one-off job that was due while the bot was down still runs, but only if it is not
# stale beyond this many seconds (12 hours).
MISSED_ONE_OFF_GRACE_SECONDS = 12 * 3600


class JobRunner(Protocol):
    """Wakes a profile's agent for a due job, delivered through its own chat."""

    async def run_scheduled_text(self, profile_name: str, text: str) -> None: ...


class SchedulerService:
    """Stores scheduled jobs through a Repository and fires them when they are due."""

    def __init__(
        self,
        repo: Repository,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._repo = repo
        self._tz = ZoneInfo(timezone)
        self._tick_seconds = tick_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    @property
    def timezone(self) -> ZoneInfo:
        """Timezone every schedule is interpreted in."""
        return self._tz

    def now(self) -> datetime:
        """Current time in the scheduler timezone."""
        return datetime.now(self._tz)

    def parse_datetime(self, raw: str) -> datetime:
        """Parse an ISO-8601 datetime; naive values are local to the timezone."""
        try:
            moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchedulerError(
                f"Invalid datetime '{raw}'. Use ISO-8601, e.g. '2026-09-10 09:30'."
            ) from exc
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self._tz)
        return moment.astimezone(self._tz)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(
        self,
        *,
        profile_name: str,
        prompt: str,
        title: str,
        at: str | None = None,
        cron: str | None = None,
    ) -> ScheduledJob:
        """Create a one-off (`at`) or repeating (`cron`) job. Exactly one of them."""
        if bool(at) == bool(cron):
            raise SchedulerError("Provide exactly one of 'at' (one-off) or 'cron' (repeating).")
        if not prompt.strip():
            raise SchedulerError("The task prompt must not be empty.")

        now = self.now()
        if cron:
            try:
                schedule = parse_cron(cron)
            except ValueError as exc:
                raise SchedulerError(str(exc)) from exc
            next_run_at = schedule.next_after(now)
            cron_expression: str | None = schedule.expression
            kind = "cron"
        else:
            next_run_at = self.parse_datetime(at or "")
            if next_run_at <= now:
                raise SchedulerError(
                    f"'{at}' is in the past. Current time is {now:%Y-%m-%d %H:%M}."
                )
            cron_expression = None
            kind = "once"

        existing = self.list_for_profile(profile_name)
        if len(existing) >= MAX_JOBS_PER_PROFILE:
            raise SchedulerError(
                f"This profile already has {len(existing)} scheduled tasks "
                f"(limit {MAX_JOBS_PER_PROFILE}). Delete some first."
            )

        job = ScheduledJob(
            id=uuid.uuid4().hex[:8],
            profile_name=profile_name,
            prompt=prompt.strip(),
            title=title.strip() or prompt.strip()[:60],
            kind=kind,
            cron=cron_expression,
            next_run_at=next_run_at,
            created_at=now,
        )
        self._repo.add_job(job)
        log.info(
            "scheduled job %s for %s (%s) next at %s",
            job.id,
            profile_name,
            job.schedule_description,
            job.next_run_at.isoformat(),
        )
        return job

    def list_for_profile(self, profile_name: str) -> list[ScheduledJob]:
        """All jobs of one profile, soonest first."""
        return self._repo.jobs_for_profile(profile_name)

    def delete(self, profile_name: str, job_id: str) -> bool:
        """Delete one job owned by profile_name. False if it does not exist."""
        job = self._repo.get_job(profile_name, job_id)
        if job is None:
            return False
        return self._repo.delete_job(job.id)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self, runner: JobRunner) -> None:
        """Start the tick loop; due jobs are handed to runner."""
        if self._loop_task is not None:
            return
        self._loop_task = asyncio.create_task(self._loop(runner))

    async def stop(self) -> None:
        """Stop the tick loop and cancel in-flight job runs."""
        tasks = [t for t in (self._loop_task, *self._running) if t is not None]
        self._loop_task = None
        self._running.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("scheduler task ended with an error during shutdown")

    async def _loop(self, runner: JobRunner) -> None:
        while True:
            try:
                self.tick(runner)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    def tick(self, runner: JobRunner) -> list[ScheduledJob]:
        """Fire every job that is due. Returns the jobs that were fired."""
        now = self.now()
        fired: list[ScheduledJob] = []

        for job in self._repo.due_jobs(now):
            # Reschedule (or delete) before running, so a slow or failing run never
            # fires twice.
            if job.kind == "cron":
                try:
                    next_run_at = parse_cron(job.cron or "").next_after(now)
                except ValueError:
                    log.warning("deleting job %s with invalid cron", job.id)
                    self._repo.delete_job(job.id)
                    continue
                self._repo.reschedule_job(job.id, next_run_at=next_run_at, last_run_at=now)
            else:
                self._repo.delete_job(job.id)
                overdue = (now - job.next_run_at).total_seconds()
                if overdue > MISSED_ONE_OFF_GRACE_SECONDS:
                    log.warning("skipping stale one-off job %s (%.0fs overdue)", job.id, overdue)
                    continue

            fired.append(job)
            task = asyncio.create_task(self._run_job(runner, job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

        return fired

    async def _run_job(self, runner: JobRunner, job: ScheduledJob) -> None:
        log.info("firing scheduled job %s for %s", job.id, job.profile_name)
        try:
            await runner.run_scheduled_text(job.profile_name, format_job_prompt(job))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled job %s failed", job.id)


def format_job_prompt(job: ScheduledJob) -> str:
    """The text delivered to the agent when a job fires."""
    return f'[Scheduled task "{job.title}"] {job.prompt}'


def format_jobs(jobs: list[ScheduledJob]) -> str:
    """Render a job list as compact text for the agent."""
    if not jobs:
        return "No scheduled tasks."
    lines = [
        f"- id={job.id} | {job.title} | {job.schedule_description} "
        f"| next: {job.next_run_at:%Y-%m-%d %H:%M} | prompt: {job.prompt}"
        for job in jobs
    ]
    return "\n".join(lines)
