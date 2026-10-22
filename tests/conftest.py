from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from claudia_core import Principal, Profile, Role
from claudia_store import SqliteStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db = SqliteStore(tmp_path / "test.sqlite3")
    yield db
    db.close()


@pytest.fixture
def seeded(store: SqliteStore) -> SqliteStore:
    """Two people: an admin with self-control, and a sister with no tools and voice replies."""
    store.sync_config(
        principals=[
            Principal(id="admin", name="Admin", role=Role.ADMIN),
            Principal(id="sister", name="Sister", role=Role.USER),
        ],
        identities=[("telegram", "111", "admin"), ("telegram", "222", "sister")],
        profiles=[
            Profile(
                name="admin",
                principal_id="admin",
                harness="claude",
                working_dir="/repo",
                tools=("Read", "Edit", "Bash"),
                model="claude-sonnet-5-5",
                models=("claude-haiku-5-5", "claude-sonnet-5-5"),
                allow_self_control=True,
            ),
            Profile(
                name="sister",
                principal_id="sister",
                harness="claude",
                working_dir="/work/sister",
                tools=(),
                model="claude-sonnet-5-5",
                models=("claude-haiku-5-5", "claude-sonnet-5-5"),
                voice_reply=True,
            ),
        ],
    )
    store.set_auth_token("admin", "token-admin")
    store.set_auth_token("sister", "token-sister")
    return store
