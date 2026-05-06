"""Tests for canonical anchor verification (D4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from symverify.canonical import (
    CanonicalCheck,
    LockdownError,
    enter_lockdown,
    load_manifest_canonical,
    verify_canonical,
)


def _write_manifest(repo: Path, anchors: list[dict]) -> None:
    (repo / "MANIFEST_CANONICAL.json").write_text(
        json.dumps({"schema": "symverify-canonical/1", "anchors": anchors}),
        encoding="utf-8",
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Empty directory acting as a repo (no .git needed for D4)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- Happy path -----------------------------------------------------------


def test_no_manifest_raises(tmp_repo: Path):
    with pytest.raises(FileNotFoundError):
        verify_canonical(tmp_repo)


def test_empty_anchors_yields_empty_results(tmp_repo: Path):
    _write_manifest(tmp_repo, [])
    assert verify_canonical(tmp_repo) == []


def test_single_anchor_match(tmp_repo: Path):
    content = b"the canonical bytes\n"
    sha = hashlib.sha256(content).hexdigest()
    (tmp_repo / "doc.md").write_bytes(content)
    _write_manifest(
        tmp_repo,
        [
            {
                "id": "doc",
                "path": "doc.md",
                "sha256": sha,
                "policy": "immutable",
                "notes": "test",
            }
        ],
    )
    [check] = verify_canonical(tmp_repo)
    assert check.ok
    assert check.expected_sha256 == sha
    assert check.got_sha256 == sha
    assert check.policy == "immutable"
    assert check.error is None


def test_drift_detected(tmp_repo: Path):
    (tmp_repo / "doc.md").write_bytes(b"actual bytes\n")
    _write_manifest(
        tmp_repo,
        [
            {
                "id": "doc",
                "path": "doc.md",
                "sha256": "0" * 64,  # wrong
                "policy": "immutable",
            }
        ],
    )
    [check] = verify_canonical(tmp_repo)
    assert not check.ok
    assert check.expected_sha256 == "0" * 64
    assert check.got_sha256 is not None
    assert check.got_sha256 != "0" * 64
    assert "DRIFT" in check.descriptor


def test_missing_file_reports_error(tmp_repo: Path):
    _write_manifest(
        tmp_repo,
        [
            {
                "id": "missing",
                "path": "nope.md",
                "sha256": "ab" * 32,
                "policy": "immutable",
            }
        ],
    )
    [check] = verify_canonical(tmp_repo)
    assert not check.ok
    assert check.error is not None
    assert check.got_sha256 is None
    assert "ERR" in check.descriptor


def test_non_immutable_anchors_are_skipped(tmp_repo: Path):
    """Anchors without policy=immutable should not be checked."""
    (tmp_repo / "advisory.md").write_bytes(b"changes welcome\n")
    _write_manifest(
        tmp_repo,
        [
            {
                "id": "advisory",
                "path": "advisory.md",
                "sha256": "0" * 64,
                "policy": "advisory",  # not immutable
            }
        ],
    )
    assert verify_canonical(tmp_repo) == []


def test_mixed_policies(tmp_repo: Path):
    """Only the immutable anchor should be checked; advisory ignored."""
    immut_bytes = b"locked\n"
    immut_sha = hashlib.sha256(immut_bytes).hexdigest()
    (tmp_repo / "locked.md").write_bytes(immut_bytes)
    (tmp_repo / "advisory.md").write_bytes(b"whatever\n")
    _write_manifest(
        tmp_repo,
        [
            {
                "id": "locked",
                "path": "locked.md",
                "sha256": immut_sha,
                "policy": "immutable",
            },
            {
                "id": "advisory",
                "path": "advisory.md",
                "sha256": "ff" * 32,
                "policy": "advisory",
            },
        ],
    )
    results = verify_canonical(tmp_repo)
    assert len(results) == 1
    assert results[0].anchor_id == "locked"
    assert results[0].ok


# --- Lockdown -------------------------------------------------------------


def test_enter_lockdown_writes_blocked_md_and_raises(tmp_path: Path):
    blocked = tmp_path / "_command" / "BLOCKED.md"
    with pytest.raises(LockdownError, match="anchor mismatch"):
        enter_lockdown("anchor mismatch on docs/foo.md", blocked)
    assert blocked.is_file()
    text = blocked.read_text(encoding="utf-8")
    # Must have the five-section structure (per Rule R19)
    for header in (
        "## 1. What just happened",
        "## 2. Why halted",
        "## 3. What is needed",
        "## 4. Where work resumes",
        "## 5. Affected",
    ):
        assert header in text, f"missing section: {header}"
    assert "anchor mismatch on docs/foo.md" in text
