"""Tests for the manifest SHA-256 cache that speeds up daemon audits.

The existing manifest tests verify that the OUTPUT of compute_local_
manifest / compute_git_manifest is byte-identical with and without
the cache. These tests focus on the cache primitives + invalidation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from symverify import manifest_cache


@pytest.fixture
def tmp_cache(tmp_path: Path):
    cache_file = tmp_path / "cache.json"
    c = manifest_cache.ManifestCache(path=cache_file)
    yield c
    # Force a save so we can assert on the file shape if desired.
    c.save(force=True)


def test_local_round_trip(tmp_cache):
    tmp_cache.put_local("/repo", "src/x.py", 12345, 100, "a" * 64)
    assert tmp_cache.get_local("/repo", "src/x.py", 12345, 100) == "a" * 64


def test_local_invalidates_on_mtime_change(tmp_cache):
    tmp_cache.put_local("/repo", "src/x.py", 12345, 100, "a" * 64)
    assert tmp_cache.get_local("/repo", "src/x.py", 12346, 100) is None


def test_local_invalidates_on_size_change(tmp_cache):
    tmp_cache.put_local("/repo", "src/x.py", 12345, 100, "a" * 64)
    assert tmp_cache.get_local("/repo", "src/x.py", 12345, 200) is None


def test_local_separate_repos_dont_collide(tmp_cache):
    tmp_cache.put_local("/repo1", "x.py", 1, 1, "aa")
    tmp_cache.put_local("/repo2", "x.py", 1, 1, "bb")
    assert tmp_cache.get_local("/repo1", "x.py", 1, 1) == "aa"
    assert tmp_cache.get_local("/repo2", "x.py", 1, 1) == "bb"


def test_git_blob_returns_size_and_sha(tmp_cache):
    tmp_cache.put_git_blob("blob_sha1_abc", 4096, "f" * 64)
    cached = tmp_cache.get_git_blob("blob_sha1_abc")
    assert cached == (4096, "f" * 64)


def test_git_blob_miss_returns_none(tmp_cache):
    assert tmp_cache.get_git_blob("nonexistent") is None


def test_persistence_round_trip(tmp_path: Path):
    """Save then reload — entries survive."""
    p = tmp_path / "cache.json"
    c1 = manifest_cache.ManifestCache(path=p)
    c1.put_local("/r", "f.py", 100, 50, "x" * 64)
    c1.put_git_blob("blob1", 50, "y" * 64)
    c1.save(force=True)

    c2 = manifest_cache.ManifestCache(path=p)
    assert c2.get_local("/r", "f.py", 100, 50) == "x" * 64
    assert c2.get_git_blob("blob1") == (50, "y" * 64)


def test_stale_schema_is_ignored(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"version": 999, "local": {}, "git_blobs": {}}),
                 encoding="utf-8")
    c = manifest_cache.ManifestCache(path=p)
    assert c.get_local("/r", "f", 0, 0) is None


def test_corrupt_json_is_ignored(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text("{not valid", encoding="utf-8")
    c = manifest_cache.ManifestCache(path=p)
    assert c._local == {}
    assert c._git == {}


def test_save_only_writes_when_dirty(tmp_path: Path):
    p = tmp_path / "cache.json"
    c = manifest_cache.ManifestCache(path=p)
    # Before any puts: save() with force=False should not create a file.
    c.save()
    assert not p.exists()
    c.put_local("/r", "f", 1, 1, "h" * 64)
    c.save(force=True)
    assert p.is_file()


def test_local_key_separator_avoids_collision(tmp_cache):
    """Two compositions that would collide under naive concatenation
    must NOT collide under the \\x1f separator."""
    # If we used colon, '/repo:x.py' + '1:1' would == '/repo' + ':x.py:1:1'
    # The current separator (\x1f, ASCII unit-separator) makes that
    # impossible because file paths can't contain it.
    tmp_cache.put_local("/repo", "x.py", 1, 1, "a" * 64)
    tmp_cache.put_local("/repo:x.py", "1", 1, 1, "b" * 64)
    assert tmp_cache.get_local("/repo", "x.py", 1, 1) == "a" * 64
    assert tmp_cache.get_local("/repo:x.py", "1", 1, 1) == "b" * 64


# ---------------------------------------------------------------------------
# Integration with manifest.compute_*  — output unchanged with cache enabled
# ---------------------------------------------------------------------------


def test_compute_local_manifest_byte_identical_with_cache(tmp_path: Path, monkeypatch):
    """Integration: compute_local_manifest must produce the same root_hash
    on a cold cache and a hot cache. If it diverges, the cache is silently
    breaking the trinity."""
    import subprocess
    from symverify import manifest

    # Make a tiny git repo with a couple of files.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_bytes(b"hello\n")
    (repo / "b.txt").write_bytes(b"world\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # Point cache at a tmp file so the global singleton doesn't pollute.
    monkeypatch.setattr(manifest_cache, "cache_path", lambda: tmp_path / "cache.json")
    manifest_cache.reset_cache()

    # Cold + hot must agree.
    cold = manifest.compute_local_manifest(repo).root_hash()
    hot = manifest.compute_local_manifest(repo).root_hash()
    assert cold == hot

    # Modify one file → both root_hashes update consistently.
    (repo / "a.txt").write_bytes(b"hello world\n")
    after = manifest.compute_local_manifest(repo).root_hash()
    assert after != cold
