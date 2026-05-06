"""Tests for `sym push` pipeline (F11.1).

We unit-test the pre-gate primitives (no git/network) and orchestrator
flow with mocked stage/commit/push functions. The full git integration
is exercised by hand during operator pushes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from symverify import push


# ---------------------------------------------------------------------------
# Pre-gate: secret scanner
# ---------------------------------------------------------------------------


def _fake_run(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                        stdout=stdout, stderr="")


def test_secret_scan_clean_diff(tmp_path: Path):
    diff = "+ added a normal line\n- removed something"
    with patch.object(push, "_run", return_value=_fake_run(stdout=diff)):
        r = push.scan_diff_for_secrets(tmp_path, scope=None)
    assert r.ok is True
    assert "no secret patterns" in r.detail


def test_secret_scan_blocks_on_github_pat(tmp_path: Path):
    leaked = "GITHUB_TOKEN = " + "gh" + "p_" + "A" * 40
    with patch.object(push, "_run", return_value=_fake_run(stdout=leaked)):
        r = push.scan_diff_for_secrets(tmp_path, scope=None)
    assert r.ok is False
    assert "matched" in r.detail


def test_secret_scan_blocks_on_aws_key(tmp_path: Path):
    leaked = "key = " + "AK" + "IA" + "Z" * 16
    with patch.object(push, "_run", return_value=_fake_run(stdout=leaked)):
        r = push.scan_diff_for_secrets(tmp_path, scope=None)
    assert r.ok is False


def test_secret_scan_blocks_on_private_key(tmp_path: Path):
    leaked = "-----BEGIN RSA PRIVATE KEY-----\nblah"
    with patch.object(push, "_run", return_value=_fake_run(stdout=leaked)):
        r = push.scan_diff_for_secrets(tmp_path, scope=None)
    assert r.ok is False


# ---------------------------------------------------------------------------
# Pre-gate: anchor verify
# ---------------------------------------------------------------------------


def test_anchor_verify_no_manifest(tmp_path: Path):
    """Repos without MANIFEST_CANONICAL.json get a free pass."""
    r = push.run_anchor_verify(tmp_path)
    assert r.ok is True


def test_anchor_verify_clean(tmp_path: Path, monkeypatch):
    fake_check = MagicMock(ok=True, path="x", expected_sha256="a" * 64,
                           got_sha256="a" * 64, error=None)
    with patch("symverify.canonical.verify_canonical", return_value=[fake_check]):
        r = push.run_anchor_verify(tmp_path)
    assert r.ok is True


def test_anchor_verify_drift(tmp_path: Path, monkeypatch):
    fake_check = MagicMock(ok=False, path="canonical.md",
                           expected_sha256="e" * 64, got_sha256="b" * 64,
                           error=None)
    with patch("symverify.canonical.verify_canonical", return_value=[fake_check]):
        r = push.run_anchor_verify(tmp_path)
    assert r.ok is False
    assert "canonical.md" in r.detail


# ---------------------------------------------------------------------------
# Repo/scope parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("reflexivity", ("reflexivity", None)),
        ("platform/", ("platform", None)),
        ("platform/apps/foo", ("platform", "apps/foo")),
        ("reflexivity/docs", ("reflexivity", "docs")),
    ],
)
def test_resolve_repo_scope(arg, expected):
    assert push.resolve_repo_scope(arg) == expected


# ---------------------------------------------------------------------------
# Orchestrator flow with mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    class _RC:
        def __init__(self, p): self.path = p
    repos = {"platform": _RC(repo_path)}

    with patch.object(push.config, "load_repos", return_value=repos):
        yield repo_path


def test_run_push_unknown_repo(fake_repo):
    with pytest.raises(push.PushError, match="not configured"):
        push.run_push("nonexistent", "msg")


def test_run_push_missing_repo_dir(tmp_path: Path):
    class _RC:
        def __init__(self, p): self.path = p
    repos = {"platform": _RC(tmp_path / "doesnotexist")}
    with patch.object(push.config, "load_repos", return_value=repos):
        with pytest.raises(push.PushError, match="path missing"):
            push.run_push("platform", "msg")


def test_run_push_nothing_to_commit(fake_repo):
    """Empty staged diff → exits with staged_count=0 and no commit."""
    with patch.object(push, "stage_changes", return_value=0), \
         patch.object(push, "run_anchor_verify",
                       return_value=push.PreGateResult(ok=True, name="anchor-verify")):
        result = push.run_push("platform", "msg",
                               skip_tests=True, skip_secret_scan=True)
    assert result.staged_count == 0
    assert result.short_sha == ""


def test_run_push_full_pipeline(fake_repo):
    """Happy path: anchor ✓ → stage 3 → secret-scan ✓ → pytest ✓ → commit + push."""
    with patch.object(push, "run_anchor_verify",
                       return_value=push.PreGateResult(ok=True, name="anchor-verify")), \
         patch.object(push, "stage_changes", return_value=3), \
         patch.object(push, "scan_diff_for_secrets",
                       return_value=push.PreGateResult(ok=True, name="secret-scan")), \
         patch.object(push, "run_pytest",
                       return_value=push.PreGateResult(ok=True, name="pytest")), \
         patch.object(push, "commit", return_value="abcdef123456"), \
         patch.object(push, "push", return_value="git@github.com:example.git"):
        result = push.run_push("platform", "feat: whatever")

    assert result.alarm() is False
    assert result.staged_count == 3
    assert result.short_sha == "abcdef123456"
    assert result.pushed_to == "git@github.com:example.git"
    # All 3 gates ran and passed.
    assert len(result.pre_gates) == 3


def test_run_push_secret_scan_aborts(fake_repo):
    """A failing secret-scan resets staging and returns alarm=True."""
    reset_called = []

    def fake_run(*args, **kw):
        reset_called.append(args[0])
        return _fake_run()

    with patch.object(push, "run_anchor_verify",
                       return_value=push.PreGateResult(ok=True, name="anchor-verify")), \
         patch.object(push, "stage_changes", return_value=2), \
         patch.object(push, "scan_diff_for_secrets",
                       return_value=push.PreGateResult(ok=False, name="secret-scan",
                                                        detail="leaked PAT")), \
         patch.object(push, "_run", side_effect=fake_run), \
         patch.object(push, "commit") as commit_mock, \
         patch.object(push, "push") as push_mock:
        result = push.run_push("platform", "msg", skip_tests=True)

    assert result.alarm() is True
    commit_mock.assert_not_called()
    push_mock.assert_not_called()
    # We tried to git reset.
    assert any("reset" in (a or []) for a in reset_called)


def test_run_push_pytest_failure_aborts(fake_repo):
    with patch.object(push, "run_anchor_verify",
                       return_value=push.PreGateResult(ok=True, name="anchor-verify")), \
         patch.object(push, "stage_changes", return_value=1), \
         patch.object(push, "scan_diff_for_secrets",
                       return_value=push.PreGateResult(ok=True, name="secret-scan")), \
         patch.object(push, "run_pytest",
                       return_value=push.PreGateResult(ok=False, name="pytest",
                                                        detail="3 failed")), \
         patch.object(push, "_run", return_value=_fake_run()), \
         patch.object(push, "commit") as commit_mock:
        result = push.run_push("platform", "msg")

    assert result.alarm() is True
    commit_mock.assert_not_called()


def test_run_push_skip_flags(fake_repo):
    """All three skip flags bypass their respective gates."""
    with patch.object(push, "stage_changes", return_value=1), \
         patch.object(push, "commit", return_value="0123456789ab"), \
         patch.object(push, "push", return_value="origin"):
        result = push.run_push("platform", "msg",
                               skip_anchor=True, skip_tests=True, skip_secret_scan=True)

    assert result.alarm() is False
    assert len(result.pre_gates) == 0
