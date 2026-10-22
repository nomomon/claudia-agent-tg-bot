from claudia_core import Principal, Profile, Role
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
