from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from claudia_core import Principal, Profile, Role, Session

# Each entry is one schema version. Append only, never edit a shipped entry.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE principals (
        id    TEXT PRIMARY KEY,
        name  TEXT NOT NULL,
        role  TEXT NOT NULL
    );
    CREATE TABLE identities (
        channel      TEXT NOT NULL,
        external_id  TEXT NOT NULL,
        principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY (channel, external_id)
    );
    CREATE TABLE profiles (
        name                TEXT PRIMARY KEY,
        principal_id        TEXT NOT NULL REFERENCES principals(id),
        harness             TEXT NOT NULL,
        working_dir         TEXT NOT NULL,
        tools               TEXT NOT NULL,
        model               TEXT,
        models              TEXT NOT NULL,
        allow_self_control  INTEGER NOT NULL,
        voice_reply         INTEGER NOT NULL,
        system_prompt       TEXT,
        auth_token          TEXT
    );
    CREATE TABLE sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_name        TEXT NOT NULL REFERENCES profiles(name),
        harness_session_id  TEXT,
        status              TEXT NOT NULL CHECK (status IN ('active', 'archived')),
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );
    CREATE INDEX sessions_active ON sessions(profile_name, status);
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            self._conn.executescript(f"BEGIN;{script}COMMIT;")
            self._conn.execute(f"PRAGMA user_version = {version}")

    # Config sync

    def sync_config(
        self,
        *,
        principals: Iterable[Principal],
        identities: Iterable[tuple[str, str, str]],
        profiles: Iterable[Profile],
    ) -> None:
        """Make the DB match the config file, without touching runtime state.

        Runtime-only fields (model after self-control, auth_token) are kept on existing rows.
        """
        self._conn.execute("BEGIN")
        try:
            for p in principals:
                self._conn.execute(
                    "INSERT INTO principals (id, name, role) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, role = excluded.role",
                    (p.id, p.name, p.role.value),
                )
            for channel, external_id, principal_id in identities:
                self._conn.execute(
                    "INSERT INTO identities (channel, external_id, principal_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(channel, external_id) "
                    "DO UPDATE SET principal_id = excluded.principal_id",
                    (channel, external_id, principal_id),
                )
            for pr in profiles:
                self._conn.execute(
                    """
                    INSERT INTO profiles (name, principal_id, harness, working_dir, tools,
                        model, models, allow_self_control, voice_reply, system_prompt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        principal_id = excluded.principal_id,
                        harness = excluded.harness,
                        working_dir = excluded.working_dir,
                        tools = excluded.tools,
                        models = excluded.models,
                        allow_self_control = excluded.allow_self_control,
                        voice_reply = excluded.voice_reply,
                        system_prompt = excluded.system_prompt
                    """,
                    (
                        pr.name,
                        pr.principal_id,
                        pr.harness,
                        pr.working_dir,
                        json.dumps(list(pr.tools)),
                        pr.model,
                        json.dumps(list(pr.models)),
                        int(pr.allow_self_control),
                        int(pr.voice_reply),
                        pr.system_prompt,
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # Lookups

    def principal_for_identity(self, channel: str, external_id: str) -> Principal | None:
        row = self._conn.execute(
            """
            SELECT p.id, p.name, p.role FROM identities i
            JOIN principals p ON p.id = i.principal_id
            WHERE i.channel = ? AND i.external_id = ?
            """,
            (channel, external_id),
        ).fetchone()
        if row is None:
            return None
        return Principal(id=row["id"], name=row["name"], role=Role(row["role"]))

    def profile_for_principal(self, principal_id: str) -> Profile | None:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE principal_id = ? ORDER BY name LIMIT 1",
            (principal_id,),
        ).fetchone()
        return _profile(row) if row else None

    def load_profile(self, profile_name: str) -> Profile:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE name = ?", (profile_name,)
        ).fetchone()
        if row is None:
            raise KeyError(profile_name)
        return _profile(row)

    # ProfileEditor (used by the harness self-control tools)

    async def get_profile(self, profile_name: str) -> Profile:
        return self.load_profile(profile_name)

    async def set_model(self, profile_name: str, model: str) -> None:
        self._conn.execute("UPDATE profiles SET model = ? WHERE name = ?", (model, profile_name))

    # Auth

    def set_auth_token(self, profile_name: str, token: str) -> None:
        cur = self._conn.execute(
            "UPDATE profiles SET auth_token = ? WHERE name = ?", (token, profile_name)
        )
        if cur.rowcount == 0:
            raise KeyError(profile_name)

    # Sessions

    def active_session(self, profile_name: str) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE profile_name = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (profile_name,),
        ).fetchone()
        return _session(row) if row else None

    def ensure_session(self, profile_name: str) -> Session:
        existing = self.active_session(profile_name)
        if existing is not None:
            return existing
        return self._insert_session(profile_name)

    def start_new_session(self, profile_name: str) -> Session:
        """Archive the active session (kept for history) and open a fresh one."""
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "UPDATE sessions SET status = 'archived', updated_at = ? "
                "WHERE profile_name = ? AND status = 'active'",
                (_now(), profile_name),
            )
            session = self._insert_session(profile_name)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return session

    def set_harness_session_id(self, session_id: int, harness_session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET harness_session_id = ?, updated_at = ? WHERE id = ?",
            (harness_session_id, _now(), session_id),
        )

    def _insert_session(self, profile_name: str) -> Session:
        now = _now()
        cur = self._conn.execute(
            "INSERT INTO sessions (profile_name, harness_session_id, status, created_at, "
            "updated_at) VALUES (?, NULL, 'active', ?, ?)",
            (profile_name, now, now),
        )
        return Session(
            id=int(cur.lastrowid or 0),
            profile_name=profile_name,
            harness_session_id=None,
            status="active",
        )


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        name=row["name"],
        principal_id=row["principal_id"],
        harness=row["harness"],
        working_dir=row["working_dir"],
        tools=tuple(json.loads(row["tools"])),
        model=row["model"],
        models=tuple(json.loads(row["models"])),
        allow_self_control=bool(row["allow_self_control"]),
        voice_reply=bool(row["voice_reply"]),
        system_prompt=row["system_prompt"],
        auth_token=row["auth_token"],
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        profile_name=row["profile_name"],
        harness_session_id=row["harness_session_id"],
        status=row["status"],
    )
