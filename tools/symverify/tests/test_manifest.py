"""Tests for manifest computation (D2).

Test vectors are pinned per `_command/08_FINGERPRINT_SPEC.md` §10.
The empty-manifest root hash is fixed by the spec; the single-file
hash was computed at D2 time from the spec example and is now frozen.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from symverify.manifest import (
    Manifest,
    ManifestEntry,
    _normalize_mode,
    _normalize_path,
    compute_git_manifest,
    compute_local_manifest,
)


# --- Test vectors from spec §10 -------------------------------------------

EMPTY_ROOT_HASH = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
SINGLE_ROOT_HASH = "03ec13d465adbc62930e25e1b9ee6d099fa91e5e4cf91792716d08293aff21ce"
HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_empty_manifest_canonical_bytes():
    m = Manifest(entries=[])
    assert m.canonical_bytes() == b"[]"


def test_empty_manifest_root_hash_matches_spec():
    m = Manifest(entries=[])
    assert m.root_hash() == EMPTY_ROOT_HASH


def test_single_entry_canonical_bytes_field_order():
    m = Manifest(
        entries=[
            ManifestEntry(
                path="README.md", mode=33188, size=5, sha256=HELLO_SHA256
            )
        ]
    )
    expected = (
        b'[{"path":"README.md","mode":33188,"size":5,'
        b'"sha256":"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}]'
    )
    assert m.canonical_bytes() == expected


def test_single_entry_root_hash_pinned():
    m = Manifest(
        entries=[
            ManifestEntry(
                path="README.md", mode=33188, size=5, sha256=HELLO_SHA256
            )
        ]
    )
    assert m.root_hash() == SINGLE_ROOT_HASH


# --- Determinism + sorting -----------------------------------------------


def test_canonical_bytes_sorts_by_path():
    """Insertion order must not affect output."""
    a = Manifest(
        entries=[
            ManifestEntry("zzz.txt", 33188, 1, "00" * 32),
            ManifestEntry("aaa.txt", 33188, 1, "11" * 32),
        ]
    )
    b = Manifest(
        entries=[
            ManifestEntry("aaa.txt", 33188, 1, "11" * 32),
            ManifestEntry("zzz.txt", 33188, 1, "00" * 32),
        ]
    )
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.root_hash() == b.root_hash()


def test_canonical_bytes_no_whitespace():
    m = Manifest(
        entries=[ManifestEntry("a.txt", 33188, 1, "00" * 32)]
    )
    blob = m.canonical_bytes()
    # No spaces or newlines anywhere.
    assert b" " not in blob
    assert b"\n" not in blob
    assert b"\t" not in blob


def test_canonical_bytes_no_trailing_newline():
    m = Manifest(entries=[])
    assert not m.canonical_bytes().endswith(b"\n")


# --- Path normalization ---------------------------------------------------


def test_normalize_path_backslashes_to_slashes():
    assert _normalize_path("src\\foo\\bar.py") == "src/foo/bar.py"


def test_normalize_path_idempotent():
    p = "docs/archive/master-original/Reflexivity_Framework_v0_42.md"
    assert _normalize_path(_normalize_path(p)) == p


# --- Mode normalization ---------------------------------------------------


def test_normalize_mode_executable_preserved():
    assert _normalize_mode(0o100755) == 0o100755


def test_normalize_mode_default_to_644():
    assert _normalize_mode(0o100644) == 0o100644
    assert _normalize_mode(0o120000) == 0o100644  # symlink coerced
    assert _normalize_mode(0o100600) == 0o100644  # weird mode coerced


# --- Live integration: against a tmp git repo ----------------------------


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Initialize a tiny git repo with two files. Returns the repo root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / "README.md").write_bytes(b"hello\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_bytes(b"print('hi')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True
    )
    return repo


def test_local_manifest_deterministic(tmp_git_repo: Path):
    m1 = compute_local_manifest(tmp_git_repo)
    m2 = compute_local_manifest(tmp_git_repo)
    assert m1.canonical_bytes() == m2.canonical_bytes()
    assert m1.root_hash() == m2.root_hash()


def test_local_and_git_manifest_match_on_clean_tree(tmp_git_repo: Path):
    """After the initial commit, local == git (no uncommitted changes)."""
    local = compute_local_manifest(tmp_git_repo)
    gitm = compute_git_manifest(tmp_git_repo)
    assert local.root_hash() == gitm.root_hash()


def test_local_diverges_from_git_when_uncommitted(tmp_git_repo: Path):
    """Editing a file but not committing should make local != git."""
    (tmp_git_repo / "README.md").write_bytes(b"changed\n")
    local = compute_local_manifest(tmp_git_repo)
    gitm = compute_git_manifest(tmp_git_repo)
    assert local.root_hash() != gitm.root_hash()


def test_local_manifest_excludes_ignored(tmp_git_repo: Path):
    """A file matching .gitignore should not appear in the local manifest."""
    (tmp_git_repo / ".gitignore").write_bytes(b"ignored.txt\n")
    (tmp_git_repo / "ignored.txt").write_bytes(b"this should not appear\n")
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "add", ".gitignore"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "commit", "-q", "-m", "add ignore"],
        check=True,
    )
    local = compute_local_manifest(tmp_git_repo)
    paths = {e.path for e in local.entries}
    assert "ignored.txt" not in paths
    assert "README.md" in paths


def test_paths_are_posix_in_manifest(tmp_git_repo: Path):
    """Path normalization should always use forward slashes."""
    local = compute_local_manifest(tmp_git_repo)
    for e in local.entries:
        assert "\\" not in e.path
