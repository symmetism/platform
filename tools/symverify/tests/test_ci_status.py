"""Tests for ci_status — GHA run polling, cache, parsing.

Network calls are mocked. The integration with the real GitHub API
is exercised by hand via the GUI's CI panel.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from symverify import ci_status


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/Symmetism/Platform.git", ("Symmetism", "Platform")),
        ("https://github.com/Symmetism/Platform", ("Symmetism", "Platform")),
        ("git@github.com:Symmetism/Reflexivity.git", ("Symmetism", "Reflexivity")),
        ("git@github.com:Symmetism/Reflexivity", ("Symmetism", "Reflexivity")),
        ("https://gitlab.com/x/y.git", None),
        ("not a url at all", None),
    ],
)
def test_parse_owner_repo(url, expected):
    assert ci_status._parse_owner_repo(url) == expected


# ---------------------------------------------------------------------------
# Duration humanizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sec,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m 0s"),
        (95, "1m 35s"),
        (3600, "1h 0m"),
        (4500, "1h 15m"),
    ],
)
def test_humanize_duration(sec, expected):
    assert ci_status.humanize_duration(sec) == expected


# ---------------------------------------------------------------------------
# RunInfo construction from raw API payload
# ---------------------------------------------------------------------------


def _api_run(**overrides):
    base = {
        "name": "Build and Publish",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "abc1234567890def",
        "created_at": "2026-05-07T12:00:00Z",
        "updated_at": "2026-05-07T12:03:00Z",
        "html_url": "https://github.com/Symmetism/Platform/actions/runs/123",
    }
    base.update(overrides)
    return base


def test_build_run_completed():
    r = ci_status._build_run("Symmetism", "Platform", _api_run())
    assert r.repo == "Platform"
    assert r.is_running is False
    assert r.head_short == "abc12345"
    assert r.conclusion == "success"
    # 12:00 → 12:03 = 180s
    assert r.elapsed_sec == 180


def test_build_run_in_progress(monkeypatch):
    """For running jobs, elapsed = now - created."""
    fixed_now = ci_status._iso_to_epoch("2026-05-07T12:01:30Z")
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    r = ci_status._build_run(
        "S", "P", _api_run(status="in_progress", conclusion=None),
    )
    assert r.is_running is True
    assert r.elapsed_sec == 90  # 12:00 → 12:01:30


# ---------------------------------------------------------------------------
# snapshot() — full path with mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_cache():
    """Clear the module-level cache between tests."""
    ci_status._CACHE = ci_status._Cache()
    yield
    ci_status._CACHE = ci_status._Cache()


def test_snapshot_no_token_returns_empty(reset_cache, monkeypatch):
    monkeypatch.setattr(ci_status, "_read_token", lambda: None)
    assert ci_status.snapshot() == []


def test_snapshot_no_repos_returns_empty(reset_cache, monkeypatch):
    monkeypatch.setattr(ci_status, "_read_token", lambda: "tok")
    monkeypatch.setattr(ci_status.config, "load_repos", lambda: {})
    assert ci_status.snapshot() == []


def test_snapshot_picks_running_run_when_present(reset_cache, monkeypatch):
    """If there's an in-progress run, snapshot picks it over the
    older completed runs."""
    monkeypatch.setattr(ci_status, "_read_token", lambda: "tok")

    repo = MagicMock()
    repo.remote = "https://github.com/Symmetism/Platform.git"
    monkeypatch.setattr(ci_status.config, "load_repos", lambda: {"platform": repo})

    runs_payload = {
        "workflow_runs": [
            _api_run(status="in_progress", conclusion=None,
                     head_sha="aaaaaa11", name="Build and Publish"),
            _api_run(status="completed", conclusion="success",
                     head_sha="bbbbbb22"),
        ]
    }

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = runs_payload
    fake_httpx = MagicMock()
    fake_httpx.get.return_value = fake_resp

    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        rows = ci_status.snapshot()
    assert len(rows) == 1
    assert rows[0].is_running is True
    assert rows[0].head_short == "aaaaaa11"


def test_snapshot_falls_back_to_latest_completed(reset_cache, monkeypatch):
    monkeypatch.setattr(ci_status, "_read_token", lambda: "tok")
    repo = MagicMock()
    repo.remote = "https://github.com/Symmetism/Platform.git"
    monkeypatch.setattr(ci_status.config, "load_repos", lambda: {"platform": repo})

    runs_payload = {
        "workflow_runs": [
            _api_run(status="completed", conclusion="success",
                     head_sha="ddddddd44"),
        ]
    }

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = runs_payload
    fake_httpx = MagicMock()
    fake_httpx.get.return_value = fake_resp

    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        rows = ci_status.snapshot()
    assert len(rows) == 1
    assert rows[0].is_running is False
    assert rows[0].conclusion == "success"


def test_snapshot_caches_within_poll_interval(reset_cache, monkeypatch):
    """Two calls inside POLL_INTERVAL_S should produce one HTTP request."""
    monkeypatch.setattr(ci_status, "_read_token", lambda: "tok")
    repo = MagicMock()
    repo.remote = "https://github.com/Symmetism/Platform.git"
    monkeypatch.setattr(ci_status.config, "load_repos", lambda: {"platform": repo})

    payload = {"workflow_runs": [_api_run()]}
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = payload
    fake_httpx = MagicMock()
    fake_httpx.get.return_value = fake_resp

    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        ci_status.snapshot()
        ci_status.snapshot()
        ci_status.snapshot()

    assert fake_httpx.get.call_count == 1


def test_snapshot_swallows_http_errors(reset_cache, monkeypatch):
    """API errors must NOT propagate to the GUI — they'd kill the
    Tk after() chain."""
    monkeypatch.setattr(ci_status, "_read_token", lambda: "tok")
    repo = MagicMock()
    repo.remote = "https://github.com/Symmetism/Platform.git"
    monkeypatch.setattr(ci_status.config, "load_repos", lambda: {"platform": repo})

    fake_httpx = MagicMock()
    fake_httpx.get.side_effect = RuntimeError("network down")

    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        rows = ci_status.snapshot()
    assert rows == []
