"""Persistent SHA-256 cache for manifest computation.

═════════════════════════════════════════════════════════════════════
CLAUDE ORIENTATION
═════════════════════════════════════════════════════════════════════
Why this exists:
  Daemon audits walk every file in both repos and SHA-256 each one.
  ~3000 files × disk read + hash on Windows = ~25-30s per audit.
  That's the dominant cost in every cycle, hourly heartbeat, and
  filesystem-trigger response. With this cache, a hot run is ~1-2s
  because most files haven't changed and we just look up their
  cached digest.

Two caches in one file:
  1. local: keyed by (repo_path, rel_path, mtime_ns, size) → sha256.
     Any file edit bumps mtime_ns or size, invalidating that entry.
     Other entries stay live.
  2. git_blobs: keyed by blob_sha1 → sha256. A git blob's SHA-1 is
     a function of its bytes, so a hit is guaranteed safe forever.

Where state lives:
  ~/.symmetism/state/manifest_cache.json (json, version-tagged).
  Wiped manually if you ever change the hash function (we never have).

Invalidation:
  - File modified → mtime_ns differs → key miss → recomputed → new
    entry inserted.
  - Old (path, *, *) entries linger; we sweep them at save time only
    if cache size grows past MAX_LOCAL_ENTRIES.
  - Manual reset: `rm ~/.symmetism/state/manifest_cache.json`.

Thread safety:
  Daemon serializes audits through one worker thread, so no lock.
  If you ever introduce concurrent audits, gate this with a Lock.

Don't:
  - Don't include this cache in any trinity computation. It's pure
    optimization; the source-of-truth is always the file bytes.
  - Don't share the cache file across machines (mtime_ns is OS-local).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from symverify import config


_VERSION = 1
MAX_LOCAL_ENTRIES = 50_000  # well above expected file count; sweep above this
SAVE_AFTER_PUTS = 200       # save to disk after this many writes


def cache_path() -> Path:
    return config.state_dir() / "manifest_cache.json"


# Module-level singleton — daemon worker is single-threaded.
_LOCK = threading.Lock()


class ManifestCache:
    """SHA-256 cache for working-tree files and git blobs."""

    def __init__(self, path: Path | None = None):
        self.path = path or cache_path()
        # value = "size,sha256_hex" — one string is faster to JSON-encode
        # and slightly smaller on disk than a [int, str] tuple.
        self._local: dict[str, str] = {}      # composite_key  → "size,sha"
        self._git: dict[str, str] = {}        # blob_sha1      → "size,sha"
        self._dirty_count = 0
        self._load()

    # ------ persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("version") != _VERSION:
            return  # stale schema; ignore
        self._local = data.get("local", {})
        self._git = data.get("git_blobs", {})

    def save(self, *, force: bool = False) -> None:
        if not force and self._dirty_count == 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Sweep oversize cache before writing.
        if len(self._local) > MAX_LOCAL_ENTRIES:
            self._local = dict(list(self._local.items())[-MAX_LOCAL_ENTRIES:])
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": _VERSION, "local": self._local, "git_blobs": self._git},
                ensure_ascii=False, separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty_count = 0

    # ------ lookup helpers -------------------------------------------------

    @staticmethod
    def _local_key(repo_path: str, rel: str, mtime_ns: int, size: int) -> str:
        # Use a separator unlikely to appear in any of the parts.
        return f"{repo_path}\x1f{rel}\x1f{mtime_ns}\x1f{size}"

    @staticmethod
    def _pack(size: int, sha: str) -> str:
        return f"{size},{sha}"

    @staticmethod
    def _unpack(packed: str) -> tuple[int, str] | None:
        try:
            s, sha = packed.split(",", 1)
            return int(s), sha
        except (ValueError, AttributeError):
            return None

    def get_local(self, repo_path: str, rel: str, mtime_ns: int, size: int) -> str | None:
        """Returns sha256 if cached. (size already known by the caller —
        keyed in the lookup, not returned, so a stale cache entry whose
        size disagrees with the on-disk file misses naturally.)"""
        v = self._local.get(self._local_key(repo_path, rel, mtime_ns, size))
        if v is None:
            return None
        unpacked = self._unpack(v)
        return unpacked[1] if unpacked else None

    def put_local(self, repo_path: str, rel: str, mtime_ns: int, size: int, sha: str) -> None:
        self._local[self._local_key(repo_path, rel, mtime_ns, size)] = self._pack(size, sha)
        self._dirty_count += 1
        if self._dirty_count >= SAVE_AFTER_PUTS:
            self.save()

    def get_git_blob(self, blob_sha1: str) -> tuple[int, str] | None:
        """Returns (size, sha256) if cached, else None. Size is needed
        on cache hit so the manifest entry can be assembled without
        re-running `git cat-file`."""
        v = self._git.get(blob_sha1)
        return self._unpack(v) if v else None

    def put_git_blob(self, blob_sha1: str, size: int, sha: str) -> None:
        self._git[blob_sha1] = self._pack(size, sha)
        self._dirty_count += 1
        if self._dirty_count >= SAVE_AFTER_PUTS:
            self.save()


# ---------------------------------------------------------------------------
# Module singleton (lazy)
# ---------------------------------------------------------------------------


_INSTANCE: ManifestCache | None = None


def get_cache() -> ManifestCache:
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = ManifestCache()
        return _INSTANCE


def reset_cache() -> None:
    """For tests. Not for production use."""
    global _INSTANCE
    with _LOCK:
        _INSTANCE = None


def hash_bytes_cached(data: bytes) -> str:
    """Hash bytes — convenience wrapper, not actually cached (we cache by
    git blob_sha1 or by (path, mtime, size), not by the bytes themselves)."""
    return hashlib.sha256(data).hexdigest()
