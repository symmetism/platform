"""SQLite persistence at `~/.symmetism/state/symverify.db` (Phase E).

Tables:
  manifests   — one row per local/git/server manifest computation
  snapshots   — one row per `sym status` audit
  events      — deploy, drift, lockdown, narrative, manual triggers
  narratives  — Haiku/OpenAI-generated text bound to a snapshot

WAL mode is enabled so concurrent reads (e.g. `sym log` while the
daemon is running) don't block writes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from symverify import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('local','git','server')),
    root_hash TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    body_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    trinity_r TEXT,
    trinity_p TEXT,
    system_fold TEXT,
    brackets_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('clean','drift','lockdown'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    repo TEXT,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narratives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    text TEXT NOT NULL,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_snapshots_taken_at ON snapshots(taken_at);
CREATE INDEX IF NOT EXISTS ix_events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS ix_narratives_generated_at ON narratives(generated_at);
"""

CURRENT_SCHEMA_VERSION = 1


def db_path() -> Path:
    return config.state_dir() / "symverify.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the SQLite DB with WAL mode + foreign keys; create schema if needed."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema setup."""
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    if row is None or row[0] is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (CURRENT_SCHEMA_VERSION, _utc_now()),
        )


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def insert_manifest(
    conn: sqlite3.Connection,
    repo: str,
    source: str,
    root_hash: str,
    body_canonical: bytes,
) -> int:
    """Record a manifest computation. body_canonical = canonical_bytes()."""
    cur = conn.execute(
        "INSERT INTO manifests(repo, source, root_hash, computed_at, body_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo, source, root_hash, _utc_now(), body_canonical.decode("utf-8", "replace")),
    )
    return cur.lastrowid


def insert_snapshot(
    conn: sqlite3.Connection,
    trinity_r: str | None,
    trinity_p: str | None,
    system_fold: str | None,
    brackets: dict[str, Any],
    status: str,
) -> int:
    """Record one full audit snapshot. status ∈ {clean, drift, lockdown}."""
    cur = conn.execute(
        "INSERT INTO snapshots(taken_at, trinity_r, trinity_p, system_fold, brackets_json, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _utc_now(),
            trinity_r,
            trinity_p,
            system_fold,
            json.dumps(brackets, ensure_ascii=False),
            status,
        ),
    )
    return cur.lastrowid


def insert_event(
    conn: sqlite3.Connection,
    kind: str,
    repo: str | None,
    detail: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events(occurred_at, kind, repo, detail_json) VALUES (?, ?, ?, ?)",
        (_utc_now(), kind, repo, json.dumps(detail or {}, ensure_ascii=False)),
    )
    return cur.lastrowid


def insert_narrative(
    conn: sqlite3.Connection,
    trigger: str,
    text: str,
    snapshot_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO narratives(generated_at, trigger, text, snapshot_id) "
        "VALUES (?, ?, ?, ?)",
        (_utc_now(), trigger, text, snapshot_id),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Read helpers (used by `sym log`)
# ---------------------------------------------------------------------------


def list_events(
    conn: sqlite3.Connection,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return events newest-first. since: ISO-8601 UTC inclusive."""
    if since:
        cur = conn.execute(
            "SELECT id, occurred_at, kind, repo, detail_json "
            "FROM events WHERE occurred_at >= ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (since, limit),
        )
    else:
        cur = conn.execute(
            "SELECT id, occurred_at, kind, repo, detail_json "
            "FROM events ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r[0],
            "occurred_at": r[1],
            "kind": r[2],
            "repo": r[3],
            "detail": json.loads(r[4]) if r[4] else {},
        }
        for r in cur.fetchall()
    ]


def list_narratives(
    conn: sqlite3.Connection,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if since:
        cur = conn.execute(
            "SELECT id, generated_at, trigger, text, snapshot_id "
            "FROM narratives WHERE generated_at >= ? "
            "ORDER BY generated_at DESC LIMIT ?",
            (since, limit),
        )
    else:
        cur = conn.execute(
            "SELECT id, generated_at, trigger, text, snapshot_id "
            "FROM narratives ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r[0],
            "generated_at": r[1],
            "trigger": r[2],
            "text": r[3],
            "snapshot_id": r[4],
        }
        for r in cur.fetchall()
    ]


def list_snapshots(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List snapshots newest-first, optionally filtered by `since` (ISO-8601 UTC)."""
    if since:
        cur = conn.execute(
            "SELECT id, taken_at, trinity_r, trinity_p, system_fold, "
            "brackets_json, status FROM snapshots "
            "WHERE taken_at >= ? "
            "ORDER BY taken_at DESC LIMIT ?",
            (since, limit),
        )
    else:
        cur = conn.execute(
            "SELECT id, taken_at, trinity_r, trinity_p, system_fold, "
            "brackets_json, status FROM snapshots "
            "ORDER BY taken_at DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r[0],
            "taken_at": r[1],
            "trinity_r": r[2],
            "trinity_p": r[3],
            "system_fold": r[4],
            "brackets": json.loads(r[5]),
            "status": r[6],
        }
        for r in cur.fetchall()
    ]


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    cur = conn.execute(
        "SELECT id, taken_at, trinity_r, trinity_p, system_fold, "
        "brackets_json, status FROM snapshots ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "taken_at": row[1],
        "trinity_r": row[2],
        "trinity_p": row[3],
        "system_fold": row[4],
        "brackets": json.loads(row[5]),
        "status": row[6],
    }
