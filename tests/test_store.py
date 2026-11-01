from datetime import UTC, datetime, timedelta

from claudia_core import Principal, Profile, Role, ScheduledJob
from claudia_store import SqliteStore


def test_identity_lookup_returns_only_the_mapped_principal(seeded: SqliteStore) -> None:
    admin = seeded.principal_for_identity("telegram", "111")
    sister = seeded.principal_for_identity("telegram", "222")
    assert admin is not None and admin.id == "admin"
    assert sister is not None and sister.id == "sister"
    assert seeded.principal_for_identity("telegram", "999") is None


async def test_sync_keeps_runtime_state(seeded: SqliteStore) -> None:
    await seeded.set_model("admin", "claude-haiku-5-5")
    seeded.sync_config(
        principals=[Principal(id="admin", name="Admin", role=Role.ADMIN)],
        identities=[],
        profiles=[
            Profile(
                name="admin",
                principal_id="admin",
                harness="claude",
                working_dir="/repo",
                tools=("Read",),
                model="claude-sonnet-5-5",
                models=("claude-haiku-5-5", "claude-sonnet-5-5"),
                allow_self_control=True,
            )
        ],
    )
    profile = seeded.load_profile("admin")
    assert profile.model == "claude-haiku-5-5"  # runtime change survives a config sync
    assert profile.auth_token == "token-admin"  # token survives too
    assert profile.tools == ("Read",)  # structural config does update


def test_start_new_session_archives_previous(seeded: SqliteStore) -> None:
    first = seeded.ensure_session("sister")
    seeded.set_harness_session_id(first.id, "hs-1")

    second = seeded.start_new_session("sister")

    assert second.id != first.id
    assert second.harness_session_id is None
    active = seeded.active_session("sister")
    assert active is not None and active.id == second.id
    assert seeded.ensure_session("sister").id == second.id


def test_sessions_are_scoped_per_profile(seeded: SqliteStore) -> None:
    admin_session = seeded.ensure_session("admin")
    sister_session = seeded.ensure_session("sister")
    assert admin_session.id != sister_session.id
    seeded.start_new_session("sister")
    still_active = seeded.active_session("admin")
    assert still_active is not None and still_active.id == admin_session.id


def test_migrations_are_idempotent(tmp_path) -> None:
    path = tmp_path / "db.sqlite3"
    SqliteStore(path).close()
    reopened = SqliteStore(path)
    assert reopened.active_session("nobody") is None
    reopened.close()


def _job(profile_name: str, **overrides) -> ScheduledJob:
    now = datetime.now(UTC)
    defaults = dict(
        id="job1",
        profile_name=profile_name,
        prompt="do the thing",
        title="Thing",
        kind="cron",
        cron="0 9 * * *",
        next_run_at=now + timedelta(hours=1),
        created_at=now,
        last_run_at=None,
    )
    defaults.update(overrides)
    return ScheduledJob(**defaults)


def test_add_and_get_job(seeded: SqliteStore) -> None:
    seeded.add_job(_job("admin"))
    job = seeded.get_job("admin", "job1")
    assert job is not None
    assert job.prompt == "do the thing"
    assert job.kind == "cron"
    assert job.last_run_at is None
    # Scoped to its own profile.
    assert seeded.get_job("sister", "job1") is None


def test_jobs_for_profile_orders_by_next_run(seeded: SqliteStore) -> None:
    now = datetime.now(UTC)
    seeded.add_job(_job("admin", id="later", next_run_at=now + timedelta(hours=2)))
    seeded.add_job(_job("admin", id="sooner", next_run_at=now + timedelta(hours=1)))
    assert [j.id for j in seeded.jobs_for_profile("admin")] == ["sooner", "later"]


def test_due_jobs_only_returns_jobs_at_or_before_now(seeded: SqliteStore) -> None:
    now = datetime.now(UTC)
    seeded.add_job(_job("admin", id="due", next_run_at=now - timedelta(minutes=1)))
    seeded.add_job(_job("admin", id="future", next_run_at=now + timedelta(hours=1)))
    assert [j.id for j in seeded.due_jobs(now)] == ["due"]


def test_reschedule_job_updates_next_and_last_run(seeded: SqliteStore) -> None:
    seeded.add_job(_job("admin"))
    now = datetime.now(UTC)
    later = now + timedelta(days=1)
    seeded.reschedule_job("job1", next_run_at=later, last_run_at=now)

    job = seeded.get_job("admin", "job1")
    assert job is not None
    assert job.next_run_at == later
    assert job.last_run_at == now


def test_delete_job_is_idempotent(seeded: SqliteStore) -> None:
    seeded.add_job(_job("admin"))
    assert seeded.delete_job("job1") is True
    assert seeded.delete_job("job1") is False
    assert seeded.get_job("admin", "job1") is None


def test_sync_config_carries_allow_scheduling(seeded: SqliteStore) -> None:
    seeded.sync_config(
        principals=[Principal(id="admin", name="Admin", role=Role.ADMIN)],
        identities=[],
        profiles=[
            Profile(
                name="admin",
                principal_id="admin",
                harness="claude",
                working_dir="/repo",
                allow_scheduling=True,
            )
        ],
    )
    assert seeded.load_profile("admin").allow_scheduling is True
