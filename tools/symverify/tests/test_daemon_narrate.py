"""Tests for the daemon's auto-narrate-on-transition hook (AI plan).

We don't actually call the OpenAI API — we patch narrative.narrate to
return a known string and assert the hook fires + persists at the
right times.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from symverify import daemon


@pytest.fixture(autouse=True)
def reset_daemon_state():
    """Reset module-level transition tracking between tests."""
    daemon._LAST_OVERALL_STATUS = None
    daemon._LAST_NARRATIVE_AT.clear()
    yield
    daemon._LAST_OVERALL_STATUS = None
    daemon._LAST_NARRATIVE_AT.clear()


SNAP = {
    "status": "drift",
    "trinity_r": "AAAA-BBBB-CCCC",
    "trinity_p": "DDDD-EEEE-FFFF",
    "system_fold": "SYM-1234-5678-9ABC-DEF0",
    "brackets": {},
}


def test_first_observation_does_not_narrate():
    """Daemon coming up with no prior status shouldn't narrate."""
    with patch.object(daemon.narrative, "narrate") as mock_narrate, \
         patch.object(daemon.db, "connect") as mock_db:
        daemon._maybe_narrate_transition("clean", SNAP, snapshot_id=1)
    mock_narrate.assert_not_called()
    mock_db.assert_not_called()


def test_no_transition_does_not_narrate():
    """clean → clean: no narrative."""
    daemon._LAST_OVERALL_STATUS = "clean"
    with patch.object(daemon.narrative, "narrate") as mock_narrate:
        daemon._maybe_narrate_transition("clean", SNAP, snapshot_id=1)
    mock_narrate.assert_not_called()


def test_clean_to_drift_narrates_once():
    daemon._LAST_OVERALL_STATUS = "clean"
    fake_text = "Two trinity legs slipped to drift_expected; brackets remain conserved."

    with patch.object(daemon.narrative, "narrate", return_value=fake_text) as mock_narrate, \
         patch.object(daemon.db, "connect") as mock_db:
        mock_db.return_value.__enter__.return_value = MagicMock()
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=42)

    mock_narrate.assert_called_once()
    # trigger string should be informative
    assert "transition" in mock_narrate.call_args.kwargs["trigger"]
    assert "clean" in mock_narrate.call_args.kwargs["trigger"]
    assert "drift" in mock_narrate.call_args.kwargs["trigger"]


def test_drift_to_lockdown_narrates():
    daemon._LAST_OVERALL_STATUS = "drift"
    with patch.object(daemon.narrative, "narrate", return_value="locked down") as mock_narrate, \
         patch.object(daemon.db, "connect"):
        daemon._maybe_narrate_transition("lockdown", SNAP, snapshot_id=1)
    mock_narrate.assert_called_once()


def test_rate_limit_blocks_repeated_narrative():
    """Two transitions to the same status within the rate-limit window
    should only produce one narrative."""
    daemon._LAST_OVERALL_STATUS = "clean"

    with patch.object(daemon.narrative, "narrate", return_value="t") as mock_narrate, \
         patch.object(daemon.db, "connect"):
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=1)

        # System flapped back to clean and then to drift again, all within
        # _NARRATIVE_RATELIMIT_SEC. We expect the second drift narrative
        # to be skipped.
        daemon._LAST_OVERALL_STATUS = "clean"
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=2)

    assert mock_narrate.call_count == 1


def test_rate_limit_window_expires():
    """If rate-limit window has elapsed, a new narrative fires."""
    daemon._LAST_OVERALL_STATUS = "clean"

    with patch.object(daemon.narrative, "narrate", return_value="t") as mock_narrate, \
         patch.object(daemon.db, "connect"):
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=1)

        # Rewind the timestamp to "long ago" to simulate the window expiring.
        daemon._LAST_NARRATIVE_AT["drift"] = time.time() - daemon._NARRATIVE_RATELIMIT_SEC - 1

        daemon._LAST_OVERALL_STATUS = "clean"
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=2)

    assert mock_narrate.call_count == 2


def test_narrate_failure_does_not_raise():
    """If narrative.narrate throws, the daemon keeps running."""
    daemon._LAST_OVERALL_STATUS = "clean"

    with patch.object(daemon.narrative, "narrate",
                       side_effect=RuntimeError("openai down")), \
         patch.object(daemon.db, "connect"):
        # Should not raise.
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=1)


def test_narrate_unavailable_text_persisted():
    """When narrate returns None, we persist a sentinel string."""
    daemon._LAST_OVERALL_STATUS = "clean"
    captured: dict = {}

    def fake_insert(conn, *, trigger, text, snapshot_id):
        captured["text"] = text
        captured["trigger"] = trigger
        captured["snapshot_id"] = snapshot_id

    with patch.object(daemon.narrative, "narrate", return_value=None), \
         patch.object(daemon.db, "connect") as mock_db, \
         patch.object(daemon.db, "insert_narrative", side_effect=fake_insert):
        mock_db.return_value.__enter__.return_value = MagicMock()
        daemon._maybe_narrate_transition("drift", SNAP, snapshot_id=99)

    assert "narrative unavailable" in captured["text"]
    assert captured["snapshot_id"] == 99
