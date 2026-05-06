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


# ---------------------------------------------------------------------------
# coherence_state — same logic as the public verify page's placeRingPoints.
# ---------------------------------------------------------------------------


def test_coherence_state_none_is_drift():
    assert gui.coherence_state(None) == "drift"


def test_coherence_state_alarm_field_wins():
    assert gui.coherence_state({"alarm": True, "status": "clean"}) == "alarm"


def test_coherence_state_lockdown_is_alarm():
    assert gui.coherence_state({"alarm": False, "status": "lockdown"}) == "alarm"


def test_coherence_state_drift():
    assert gui.coherence_state({"alarm": False, "status": "drift"}) == "drift"


def test_coherence_state_clean_is_aligned():
    assert gui.coherence_state({"alarm": False, "status": "clean"}) == "aligned"


def test_coherence_state_unknown_status_is_drift():
    assert gui.coherence_state({"alarm": False, "status": "weird"}) == "drift"


# ---------------------------------------------------------------------------
# TrinityRings position table — must match verify.js exactly.
# ---------------------------------------------------------------------------


def test_trinity_rings_position_table_matches_verify_page():
    """If these positions drift from the verify page, the in-app and
    in-browser indicators will tell different stories."""
    assert gui.TrinityRings.DOT_POSITIONS["aligned"] == [(0, 0), (0, 0), (0, 0)]
    assert gui.TrinityRings.DOT_POSITIONS["drift"] == [(-12, 0), (12, 0), (0, 0)]
    assert gui.TrinityRings.DOT_POSITIONS["alarm"] == [
        (-50, 30), (50, 30), (0, -55),
    ]


def test_trinity_rings_color_table_matches_verify_page():
    assert gui.TrinityRings.DOT_FILL["aligned"] == "#7eb6d9"
    assert gui.TrinityRings.DOT_FILL["drift"] == "#e0a458"
    assert gui.TrinityRings.DOT_FILL["alarm"] == "#cc4444"


# ---------------------------------------------------------------------------
# PlatonicCycle — vertex/edge tables must match Euler's formula V−E+F=2.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,fn,expected_vertices,expected_edges",
    [
        ("tetrahedron",  gui.PlatonicCycle._tetrahedron,  4, 6),
        ("cube",         gui.PlatonicCycle._cube,         8, 12),
        ("octahedron",   gui.PlatonicCycle._octahedron,   6, 12),
        ("dodecahedron", gui.PlatonicCycle._dodecahedron, 20, 30),
        ("icosahedron",  gui.PlatonicCycle._icosahedron,  12, 30),
    ],
)
def test_platonic_solid_topology_is_correct(name, fn, expected_vertices, expected_edges):
    """Each Platonic solid has a known fixed (V, E) pair."""
    verts, edges = fn()
    assert len(verts) == expected_vertices, f"{name}: V mismatch"
    assert len(edges) == expected_edges, f"{name}: E mismatch"
    # Edges must reference valid vertex indices.
    for a, b in edges:
        assert 0 <= a < len(verts) and 0 <= b < len(verts), \
            f"{name}: edge {(a, b)} references missing vertex"
    # No self-loops, no duplicate edges (treating undirected).
    edge_set = {tuple(sorted(e)) for e in edges}
    assert len(edge_set) == len(edges), f"{name}: duplicate edges"
    assert all(a != b for a, b in edges), f"{name}: self-loop edge"


def test_platonic_cycle_lists_five_shapes():
    names = [name for name, _ in gui.PlatonicCycle.SHAPES]
    assert names == [
        "tetrahedron", "cube", "octahedron", "dodecahedron", "icosahedron",
    ]


def test_platonic_shape_descriptors_callable_bare():
    """Regression: PlatonicCycle._draw pulls bare descriptors from
    SHAPES and calls them — `fn = self.SHAPES[i][1]; fn()`. If a shape
    was @classmethod (or anything that needs class-attribute binding),
    this would TypeError silently inside Tk's after() callback chain
    and freeze the animation on the last successful frame for several
    seconds at a time. All five shape methods must be @staticmethod
    or otherwise callable bare.
    """
    for name, fn in gui.PlatonicCycle.SHAPES:
        try:
            v, e = fn()  # exact same pattern as _draw
        except TypeError as exc:
            pytest.fail(
                f"{name}: bare descriptor not callable — {exc}. "
                f"Must be @staticmethod, not @classmethod."
            )
        assert len(v) > 0 and len(e) > 0, f"{name}: empty geometry"
