"""Tests for GUI helpers (data loading, formatting).

We don't import customtkinter here so the test suite stays headless;
SymGUI/SettingsWindow are exercised by hand. The module-level helpers
(`load_status`, `humanize_age`, `_status_color`, `_status_glyph`) are
where the meaningful logic lives, so that's what we cover.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# customtkinter pulls in Tk on import. Skip the whole module if it's
# missing — gui is an optional extra (see pyproject.toml [gui]).
ctk = pytest.importorskip("customtkinter")

from symverify import gui  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_status_glyph_known_states():
    assert gui._status_glyph("conserved") == "✓"
    assert gui._status_glyph("drift_expected") == "⚠"
    assert gui._status_glyph("drift") == "✗"
    assert gui._status_glyph("lockdown") == "✗"
    assert gui._status_glyph("drift_alarm") == "✗"
    assert gui._status_glyph("unknown") == "·"


def test_status_color_known_states():
    assert gui._status_color("conserved") == gui.COLOR_STABLE
    assert gui._status_color("drift_expected") == gui.COLOR_DRIFT
    assert gui._status_color("drift") == gui.COLOR_DRIFT
    assert gui._status_color("lockdown") == gui.COLOR_ALARM
    assert gui._status_color("drift_alarm") == gui.COLOR_ALARM
    assert gui._status_color("???") == gui.COLOR_MUTED


def test_humanize_age_seconds():
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "s ago" in gui.humanize_age(ts)


def test_humanize_age_minutes():
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "m ago" in gui.humanize_age(ts)


def test_humanize_age_hours():
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "h ago" in gui.humanize_age(ts)


def test_humanize_age_days():
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "d ago" in gui.humanize_age(ts)


def test_humanize_age_just_now_for_future():
    now = datetime.now(timezone.utc)
    ts = (now + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert gui.humanize_age(ts) == "just now"


def test_humanize_age_invalid_string_returned_verbatim():
    assert gui.humanize_age("not-a-timestamp") == "not-a-timestamp"


# ---------------------------------------------------------------------------
# load_status
# ---------------------------------------------------------------------------


def test_load_status_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gui, "status_path", lambda: tmp_path / "missing.json")
    assert gui.load_status() is None


def test_load_status_malformed(tmp_path: Path, monkeypatch):
    p = tmp_path / "status.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(gui, "status_path", lambda: p)
    assert gui.load_status() is None


def test_load_status_round_trip(tmp_path: Path, monkeypatch):
    p = tmp_path / "status.json"
    payload = {"system_fold": "SYM-AAAA-BBBB-CCCC-DDDD", "status": "drift"}
    p.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(gui, "status_path", lambda: p)
    got = gui.load_status()
    assert got == payload
