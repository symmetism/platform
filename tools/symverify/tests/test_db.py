"""Tests for SQLite persistence (E1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from symverify import db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "symverify.db"


def test_connect_creates_schema(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {r[0] for r in cur.fetchall()}
    assert {"manifests", "snapshots", "events", "narratives", "schema_version"} <= tables


def test_connect_is_idempotent(tmp_db: Path):
    with db.connect(tmp_db):
        pass
    with db.connect(tmp_db) as conn:
        cur = conn.execute("SELECT version FROM schema_version")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == db.CURRENT_SCHEMA_VERSION


def test_wal_mode_enabled(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        cur = conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
    assert mode.lower() == "wal"


def test_insert_and_read_snapshot(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        snap_id = db.insert_snapshot(
            conn,
            trinity_r="T9K2-MQ4N-XR8P",
            trinity_p="B7H4-NK2X-YR5Q",
            system_fold="SYM-AAAA-BBBB-CCCC-DDDD",
            brackets={"Q_canonical": {"value": 0, "status": "conserved"}},
            status="clean",
        )
    assert isinstance(snap_id, int) and snap_id > 0
    with db.connect(tmp_db) as conn:
        latest = db.latest_snapshot(conn)
    assert latest["trinity_r"] == "T9K2-MQ4N-XR8P"
    assert latest["status"] == "clean"
    assert latest["brackets"]["Q_canonical"]["value"] == 0


def test_insert_and_list_events(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        db.insert_event(conn, kind="manual", repo="reflexivity", detail={"note": "init"})
        db.insert_event(conn, kind="drift", repo=None, detail={"charge": "Q_trinity_R"})
    with db.connect(tmp_db) as conn:
        events = db.list_events(conn, limit=10)
    assert len(events) == 2
    # newest-first
    assert events[0]["kind"] == "drift"
    assert events[1]["kind"] == "manual"
    assert events[1]["detail"]["note"] == "init"


def test_insert_and_list_narratives(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        snap_id = db.insert_snapshot(
            conn,
            trinity_r=None,
            trinity_p=None,
            system_fold=None,
            brackets={},
            status="clean",
        )
        db.insert_narrative(
            conn,
            trigger="manual",
            text="All systems nominal.",
            snapshot_id=snap_id,
        )
    with db.connect(tmp_db) as conn:
        nars = db.list_narratives(conn)
    assert len(nars) == 1
    assert nars[0]["trigger"] == "manual"
    assert nars[0]["snapshot_id"] == snap_id


def test_insert_manifest(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        mid = db.insert_manifest(
            conn,
            repo="reflexivity",
            source="git",
            root_hash="a" * 64,
            body_canonical=b'[{"path":"a.txt","mode":33188,"size":1,"sha256":"00"}]',
        )
    assert isinstance(mid, int)


def test_status_check_constraint(tmp_db: Path):
    """`status` must be one of clean/drift/lockdown."""
    with db.connect(tmp_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO snapshots(taken_at, brackets_json, status) "
                "VALUES (?, ?, ?)",
                ("2026-01-01T00:00:00Z", "{}", "BOGUS_STATUS"),
            )


def test_source_check_constraint(tmp_db: Path):
    """`source` must be one of local/git/server."""
    with db.connect(tmp_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO manifests(repo, source, root_hash, computed_at, body_json) "
                "VALUES (?, ?, ?, ?, ?)",
                ("r", "BOGUS", "0" * 64, "2026-01-01T00:00:00Z", "[]"),
            )


def test_list_events_since_filter(tmp_db: Path):
    with db.connect(tmp_db) as conn:
        # Manually set occurred_at to control timing
        conn.execute(
            "INSERT INTO events(occurred_at, kind, repo, detail_json) "
            "VALUES ('2026-01-01T00:00:00Z', 'manual', 'r', '{}')"
        )
        conn.execute(
            "INSERT INTO events(occurred_at, kind, repo, detail_json) "
            "VALUES ('2026-06-01T00:00:00Z', 'drift', NULL, '{}')"
        )
    with db.connect(tmp_db) as conn:
        recent = db.list_events(conn, since="2026-03-01T00:00:00Z")
    assert len(recent) == 1
    assert recent[0]["kind"] == "drift"
