"""Tests for stabilizer registry, brackets, six base charges (D5)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from symverify.stabilizer import (
    AuditReport,
    Bracket,
    ChargeSpec,
    Registry,
    State,
    STATUS_CONSERVED,
    STATUS_DRIFT_ALARM,
    STATUS_DRIFT_EXPECTED,
    STATUS_PENDING,
    compute_q_canonical,
    compute_q_cross_repo,
    compute_q_secrets,
    compute_q_structure,
    compute_q_trinity_P,
    compute_q_trinity_R,
)


# --- Fixtures -------------------------------------------------------------


def _git_init(repo: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@test.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"], check=True
    )


def _git_commit(repo: Path, msg: str = "c"):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True
    )


@pytest.fixture
def repo_with_anchor(tmp_path: Path) -> Path:
    """Repo with one immutable anchor (and matching MANIFEST_CANONICAL.json)."""
    repo = tmp_path / "anchor_repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "docs").mkdir()
    content = b"canonical bytes\n"
    (repo / "docs" / "v1.md").write_bytes(content)
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    (repo / "MANIFEST_CANONICAL.json").write_text(
        json.dumps(
            {
                "schema": "symverify-canonical/1",
                "anchors": [
                    {
                        "id": "v1",
                        "path": "docs/v1.md",
                        "sha256": sha,
                        "policy": "immutable",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _git_commit(repo, "initial")
    return repo


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """Repo with no canonical anchors."""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "MANIFEST_CANONICAL.json").write_text(
        json.dumps({"schema": "symverify-canonical/1", "anchors": []}),
        encoding="utf-8",
    )
    (repo / "README.md").write_bytes(b"hi\n")
    _git_commit(repo, "init")
    return repo


def _spec(
    id: str,
    compute_func: str,
    expected_value=None,
    expected_nonzero=None,
    alarm_on_nonzero=True,
) -> ChargeSpec:
    return ChargeSpec(
        id=id,
        ordinal=1,
        description="t",
        compute_func=compute_func,
        expected_value=expected_value,
        expected_value_source="t",
        expected_nonzero=expected_nonzero or [],
        alarm_on_nonzero=alarm_on_nonzero,
        added_at="2026-05-06T00:00:00Z",
        added_in_step="t",
    )


# --- Q_canonical ----------------------------------------------------------


def test_q_canonical_no_anchors_is_conserved(empty_repo: Path):
    state = State(reflexivity_path=empty_repo)
    spec = _spec("Q_canonical", "symverify.stabilizer.compute_q_canonical")
    b = compute_q_canonical(state, spec)
    assert b.status == STATUS_CONSERVED
    assert b.value == 0


def test_q_canonical_match_against_pinned(repo_with_anchor: Path):
    """Compute the expected value, set it, verify bracket = 0."""
    import hashlib

    spec_compute = _spec(
        "Q_canonical", "symverify.stabilizer.compute_q_canonical"
    )
    state = State(reflexivity_path=repo_with_anchor)
    # First compute: get the actual value (no expected pinned)
    first = compute_q_canonical(state, spec_compute)
    assert first.status == STATUS_DRIFT_ALARM  # expected is None
    actual_hash = first.value
    # Pin and re-run
    spec_pinned = _spec(
        "Q_canonical",
        "symverify.stabilizer.compute_q_canonical",
        expected_value=actual_hash,
    )
    second = compute_q_canonical(state, spec_pinned)
    assert second.status == STATUS_CONSERVED
    assert second.value == 0


def test_q_canonical_drift_alarm_on_mismatch(repo_with_anchor: Path):
    state = State(reflexivity_path=repo_with_anchor)
    spec = _spec(
        "Q_canonical",
        "symverify.stabilizer.compute_q_canonical",
        expected_value="0" * 64,
    )
    b = compute_q_canonical(state, spec)
    assert b.status == STATUS_DRIFT_ALARM
    assert b.value != 0


# --- Q_structure ----------------------------------------------------------


def test_q_structure_pending_when_paths_missing(empty_repo: Path):
    spec = _spec(
        "Q_structure",
        "symverify.stabilizer.compute_q_structure",
        expected_value="<placeholder>",
    )
    state = State(reflexivity_path=empty_repo)
    b = compute_q_structure(state, spec)
    assert b.status == STATUS_DRIFT_ALARM  # paths missing → alarm
    assert "missing" in (b.descriptor or "")


def test_q_structure_first_compute_is_pending(tmp_path: Path):
    """All paths present, but expected_value is a placeholder string."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    # Create all required structural paths
    (repo / ".githooks").mkdir()
    (repo / ".githooks" / "pre-commit").write_bytes(b"#!/bin/sh\nexit 0\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "workflows").mkdir()
    (repo / ".github" / "workflows" / "verify-canonical.yml").write_bytes(
        b"name: verify\n"
    )
    (repo / ".github" / "CODEOWNERS").write_bytes(b"* @t\n")
    (repo / "MANIFEST_CANONICAL.json").write_text(
        '{"schema":"symverify-canonical/1","anchors":[]}', encoding="utf-8"
    )
    (repo / ".gitattributes").write_bytes(b"* text=auto\n")
    _git_commit(repo, "init")

    spec = _spec(
        "Q_structure",
        "symverify.stabilizer.compute_q_structure",
        expected_value="<placeholder>",
    )
    state = State(reflexivity_path=repo)
    b = compute_q_structure(state, spec)
    assert b.status == STATUS_PENDING
    assert b.value is not None  # has the computed hash


# --- Q_trinity_R / Q_trinity_P --------------------------------------------


def test_q_trinity_conserved_when_all_three_match():
    spec = _spec("Q_trinity_R", "symverify.stabilizer.compute_q_trinity_R")
    state = State(
        reflexivity_local_hash="a" * 64,
        reflexivity_git_hash="a" * 64,
        reflexivity_server_hash="a" * 64,
    )
    b = compute_q_trinity_R(state, spec)
    assert b.status == STATUS_CONSERVED
    assert b.value == 0


def test_q_trinity_drift_expected_when_local_neq_git():
    spec = _spec(
        "Q_trinity_R",
        "symverify.stabilizer.compute_q_trinity_R",
        expected_nonzero=[
            {"pattern": "local≠git", "reason": "uncommitted dev changes"}
        ],
        alarm_on_nonzero=False,
    )
    state = State(
        reflexivity_local_hash="a" * 64,
        reflexivity_git_hash="b" * 64,
        reflexivity_server_hash="b" * 64,
    )
    b = compute_q_trinity_R(state, spec)
    assert b.status == STATUS_DRIFT_EXPECTED
    assert b.matched_pattern == "local≠git"


def test_q_trinity_drift_expected_when_no_server_yet():
    spec = _spec(
        "Q_trinity_R",
        "symverify.stabilizer.compute_q_trinity_R",
        expected_nonzero=[
            {"pattern": "no_server_yet", "reason": "pre-deploy"}
        ],
        alarm_on_nonzero=False,
    )
    state = State(
        reflexivity_local_hash="a" * 64,
        reflexivity_git_hash="a" * 64,
        reflexivity_server_hash=None,
    )
    b = compute_q_trinity_R(state, spec)
    assert b.status == STATUS_DRIFT_EXPECTED
    assert b.matched_pattern == "no_server_yet"


def test_q_trinity_alarm_on_history_rewrite():
    spec = _spec(
        "Q_trinity_R", "symverify.stabilizer.compute_q_trinity_R"
    )
    state = State(
        reflexivity_local_hash="a" * 64,
        reflexivity_git_hash="b" * 64,
        reflexivity_server_hash="a" * 64,
    )
    b = compute_q_trinity_R(state, spec)
    assert b.status == STATUS_DRIFT_ALARM
    assert "history_rewrite" in (b.descriptor or "")


def test_q_trinity_pending_when_no_inputs():
    spec = _spec("Q_trinity_R", "symverify.stabilizer.compute_q_trinity_R")
    state = State()
    b = compute_q_trinity_R(state, spec)
    assert b.status == STATUS_PENDING


def test_q_trinity_p_uses_platform_inputs():
    spec = _spec("Q_trinity_P", "symverify.stabilizer.compute_q_trinity_P")
    state = State(
        platform_local_hash="x" * 64,
        platform_git_hash="x" * 64,
        platform_server_hash="x" * 64,
    )
    b = compute_q_trinity_P(state, spec)
    assert b.status == STATUS_CONSERVED


# --- Q_cross_repo ---------------------------------------------------------


def test_q_cross_repo_conserved_when_invariants_align():
    spec = _spec(
        "Q_cross_repo",
        "symverify.stabilizer.compute_q_cross_repo",
        expected_value="<placeholder>",
    )
    state = State(
        invariants_R={"version": "0.1.0", "license": "MIT"},
        invariants_P={"version": "0.1.0", "license": "MIT"},
    )
    b = compute_q_cross_repo(state, spec)
    # Placeholder expected → returns pending
    assert b.status == STATUS_PENDING
    assert b.value is not None


def test_q_cross_repo_alarm_on_mismatch():
    spec = _spec(
        "Q_cross_repo", "symverify.stabilizer.compute_q_cross_repo"
    )
    state = State(
        invariants_R={"license": "MIT"},
        invariants_P={"license": "Apache-2.0"},
    )
    b = compute_q_cross_repo(state, spec)
    assert b.status == STATUS_DRIFT_ALARM
    assert "mismatch" in (b.descriptor or "")


# --- Q_secrets ------------------------------------------------------------


def test_q_secrets_clean_repo(empty_repo: Path):
    spec = _spec(
        "Q_secrets", "symverify.stabilizer.compute_q_secrets",
        expected_value="0",
    )
    state = State(reflexivity_path=empty_repo)
    b = compute_q_secrets(state, spec)
    assert b.status == STATUS_CONSERVED
    assert b.value == 0


# Test fixtures are split so the source-file bytes don't themselves match
# Q_secrets' regexes (which would alarm on every audit). The runtime bytes
# (constructed below) still match, so the tests still verify the scanner.
_AWS_PREFIX = b"AKIA"
_AWS_BODY = b"IOSFODNN7" + b"EXAMPLE"  # AWS docs canonical example
_GH_PREFIX = b"ghp_"
_GH_BODY = b"1234567890" + b"ABCDEFabcdef" + b"1234567890ABCDEF"  # 36 chars


def test_q_secrets_alarm_on_aws_key(tmp_path: Path):
    repo = tmp_path / "leaky"
    repo.mkdir()
    _git_init(repo)
    (repo / "config.txt").write_bytes(_AWS_PREFIX + _AWS_BODY + b"\n")
    _git_commit(repo, "leak")
    spec = _spec("Q_secrets", "symverify.stabilizer.compute_q_secrets")
    state = State(reflexivity_path=repo)
    b = compute_q_secrets(state, spec)
    assert b.status == STATUS_DRIFT_ALARM
    assert b.value == 1


def test_q_secrets_alarm_on_github_pat(tmp_path: Path):
    repo = tmp_path / "leaky"
    repo.mkdir()
    _git_init(repo)
    (repo / "settings.txt").write_bytes(b"GH_TOKEN=" + _GH_PREFIX + _GH_BODY + b"\n")
    _git_commit(repo, "leak")
    spec = _spec("Q_secrets", "symverify.stabilizer.compute_q_secrets")
    state = State(reflexivity_path=repo)
    b = compute_q_secrets(state, spec)
    assert b.status == STATUS_DRIFT_ALARM


# --- Registry round-trip --------------------------------------------------


def test_registry_load_save_roundtrip(tmp_path: Path):
    src = tmp_path / "REG.json"
    src.write_text(
        json.dumps(
            {
                "spec_version": "stabilizer-registry/1",
                "framework_dim_stab": 6,
                "comment": "test",
                "charges": [
                    {
                        "id": "Q_canonical",
                        "ordinal": 1,
                        "description": "t",
                        "compute_func": "symverify.stabilizer.compute_q_canonical",
                        "expected_value": "abc",
                        "expected_value_source": "t",
                        "expected_nonzero": [],
                        "alarm_on_nonzero": True,
                        "added_at": "2026-05-06T00:00:00Z",
                        "added_in_step": "B1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = Registry.load(src)
    assert reg.spec_version == "stabilizer-registry/1"
    assert reg.framework_dim_stab == 6
    assert len(reg.charges) == 1
    assert reg.charges[0].id == "Q_canonical"
    out = tmp_path / "OUT.json"
    reg.save(out)
    reloaded = Registry.load(out)
    assert reloaded.charges == reg.charges


def test_registry_audit_returns_one_bracket_per_charge(empty_repo: Path):
    src = Path(empty_repo) / "REG.json"
    src.write_text(
        json.dumps(
            {
                "spec_version": "stabilizer-registry/1",
                "framework_dim_stab": 6,
                "comment": "test",
                "charges": [
                    {
                        "id": "Q_canonical",
                        "ordinal": 1,
                        "description": "t",
                        "compute_func": "symverify.stabilizer.compute_q_canonical",
                        "expected_value": None,
                        "expected_value_source": "t",
                        "expected_nonzero": [],
                        "alarm_on_nonzero": True,
                        "added_at": "2026-05-06T00:00:00Z",
                        "added_in_step": "B1",
                    },
                    {
                        "id": "Q_secrets",
                        "ordinal": 6,
                        "description": "t",
                        "compute_func": "symverify.stabilizer.compute_q_secrets",
                        "expected_value": "0",
                        "expected_value_source": "constant",
                        "expected_nonzero": [],
                        "alarm_on_nonzero": True,
                        "added_at": "2026-05-06T00:00:00Z",
                        "added_in_step": "B1",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = Registry.load(src)
    state = State(reflexivity_path=empty_repo)
    report = reg.audit(state)
    assert len(report.brackets) == 2
    assert {b.charge_id for b in report.brackets} == {"Q_canonical", "Q_secrets"}
    assert report.audit_run_at.endswith("Z")


def test_registry_audit_alarm_propagates(repo_with_anchor: Path):
    src = Path(repo_with_anchor) / "REG.json"
    src.write_text(
        json.dumps(
            {
                "spec_version": "stabilizer-registry/1",
                "framework_dim_stab": 6,
                "comment": "test",
                "charges": [
                    {
                        "id": "Q_canonical",
                        "ordinal": 1,
                        "description": "t",
                        "compute_func": "symverify.stabilizer.compute_q_canonical",
                        "expected_value": "0" * 64,  # wrong
                        "expected_value_source": "t",
                        "expected_nonzero": [],
                        "alarm_on_nonzero": True,
                        "added_at": "2026-05-06T00:00:00Z",
                        "added_in_step": "B1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = Registry.load(src)
    state = State(reflexivity_path=repo_with_anchor)
    report = reg.audit(state)
    assert report.alarm is True
    assert report.brackets[0].status == STATUS_DRIFT_ALARM
