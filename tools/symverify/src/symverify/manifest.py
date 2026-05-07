"""Manifest computation per `_command/08_FINGERPRINT_SPEC.md`.

A manifest is a sorted list of (path, mode, size, sha256) entries. Its
canonical bytes are deterministic JSON; the root_hash is SHA-256 of
those bytes. The spec is frozen; implementations must match byte-for-byte.

Three scopes:
  - local manifest:  working tree (git's view of tracked + untracked-not-
                     ignored)
  - git manifest:    `git ls-tree -r HEAD` plus blob bytes for SHA-256/size
  - server manifest: returned by deployed `/__manifest` (Phase F)
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from symverify import git_ops


def _normalize_path(path: str) -> str:
    """Path normalization per spec §2.

    1. backslashes -> forward slashes
    2. NFC-normalize Unicode codepoints
    3. (root-stripping is the caller's responsibility — paths arrive
       already-relative from git ls-files / ls-tree)
    """
    return unicodedata.normalize("NFC", path.replace("\\", "/"))


def _normalize_mode(mode: int) -> int:
    """Mask reported modes to one of 0o100755 (executable) or 0o100644.

    Anything else (e.g. 0o120000 symlink — git stores symlinks as blobs
    of the target path with a special mode) we coerce to 0o100644.
    Submodules (0o160000) are excluded upstream by `git_ls_tree`.
    """
    if mode == 0o100755:
        return 0o100755
    return 0o100644


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """A single file entry in a manifest (spec §3)."""

    path: str
    mode: int
    size: int
    sha256: str

    def to_dict(self) -> dict:
        # Field ORDER is part of the spec — must be path, mode, size, sha256.
        # Python 3.7+ dicts preserve insertion order; json.dumps respects it
        # unless sort_keys=True.
        return {
            "path": self.path,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(slots=True)
class Manifest:
    """A list of ManifestEntries; deterministic-encodable.

    Construction does not sort; `canonical_bytes()` does. The freezing of
    sort order at encode time means callers can build entries in any order.
    """

    entries: list[ManifestEntry]

    def canonical_bytes(self) -> bytes:
        """Spec §4 canonical encoding.

        Sort entries by path lex byte-wise (UTF-8 byte order matches
        Python's default str compare for ASCII; for non-ASCII we sort by
        UTF-8 bytes explicitly). JSON: no whitespace, fixed field order,
        no trailing newline.
        """
        sorted_entries = sorted(
            self.entries, key=lambda e: e.path.encode("utf-8")
        )
        payload = [e.to_dict() for e in sorted_entries]
        # ensure_ascii=False keeps non-ASCII bytes literal (spec §4: not
        # escaped). separators=(",", ":") emits no whitespace.
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def root_hash(self) -> str:
        """SHA-256 hex of canonical_bytes (spec §5)."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Local manifest
# ---------------------------------------------------------------------------


def compute_local_manifest(repo_path: str | Path) -> Manifest:
    """Build a Manifest from the working tree.

    Files visible: tracked + untracked-not-ignored, per
    `git ls-files --cached --others --exclude-standard`. Each file's
    SHA-256 is computed from the bytes on disk. Modes come from git's
    index for tracked files; untracked default to 0o100644.

    Caller passes the repo root (anywhere git can resolve `--show-toplevel`
    is fine — we re-resolve to the toplevel internally).

    Performance: SHA-256 is cached by (repo_path, rel, mtime_ns, size).
    When a file's mtime/size match the cached entry, the cached SHA is
    reused without re-reading the bytes. Reduces audit time on Windows
    from ~25s to ~1-2s on hot runs (most files don't change). See
    manifest_cache.py for invalidation semantics.
    """
    from symverify import manifest_cache

    cache = manifest_cache.get_cache()
    repo = Path(git_ops.git_root(repo_path))
    repo_str = str(repo)
    paths = git_ops.git_ls_files_local(repo)
    index_modes = git_ops.git_ls_files_stage(repo)

    entries: list[ManifestEntry] = []
    for rel in paths:
        norm = _normalize_path(rel)
        full = repo / rel
        # Defensive: the path may be a directory if a submodule slipped in;
        # git ls-files normally only emits blobs but be safe.
        if not full.is_file():
            continue
        st = full.stat()
        size = st.st_size
        mtime_ns = st.st_mtime_ns
        mode = _normalize_mode(index_modes.get(rel, 0o100644))

        sha = cache.get_local(repo_str, rel, mtime_ns, size)
        if sha is None:
            data = full.read_bytes()
            # Defensive: stat-reported size and actual byte count must
            # agree — if not, file was rewritten between stat and read.
            # Skip cache write to force a recompute next time.
            if len(data) != size:
                size = len(data)
                sha = hashlib.sha256(data).hexdigest()
            else:
                sha = hashlib.sha256(data).hexdigest()
                cache.put_local(repo_str, rel, mtime_ns, size, sha)
        entries.append(ManifestEntry(path=norm, mode=mode, size=size, sha256=sha))
    cache.save()
    return Manifest(entries=entries)


# ---------------------------------------------------------------------------
# Git manifest
# ---------------------------------------------------------------------------


def compute_git_manifest(repo_path: str | Path, ref: str = "HEAD") -> Manifest:
    """Build a Manifest from `git ls-tree -r <ref>`.

    For each blob entry: read its bytes via `git cat-file blob <sha>`,
    compute size + SHA-256 from those bytes (the SHA-256 in the manifest
    is over file content, NOT git's blob SHA-1). Mode is git's reported
    tree mode.

    Performance: keyed by git's blob SHA-1 (which IS a function of the
    bytes), the SHA-256 is cached forever — a hit avoids both the
    `git cat-file` subprocess and the hash. On hot runs, this turns
    most of compute_git_manifest() into a hashmap lookup. See
    manifest_cache.py.
    """
    from symverify import manifest_cache

    cache = manifest_cache.get_cache()
    repo = Path(git_ops.git_root(repo_path))
    entries: list[ManifestEntry] = []
    for mode, blob_sha, path in git_ops.git_ls_tree(repo, ref):
        norm = _normalize_path(path)
        cached = cache.get_git_blob(blob_sha)
        if cached is not None:
            size, sha = cached
        else:
            data = git_ops.git_cat_file_blob(repo, blob_sha)
            size = len(data)
            sha = hashlib.sha256(data).hexdigest()
            cache.put_git_blob(blob_sha, size, sha)
        entries.append(
            ManifestEntry(path=norm, mode=_normalize_mode(mode), size=size, sha256=sha)
        )
    cache.save()
    return Manifest(entries=entries)


# ---------------------------------------------------------------------------
# Server manifest (third trinity leg)
# ---------------------------------------------------------------------------


class ServerManifestError(RuntimeError):
    """Raised when /__manifest can't be fetched or parsed."""


def compute_server_manifest(
    server_url: str,
    token: str,
    *,
    manifest_path: str = "/__manifest",
    timeout: float = 5.0,
) -> dict:
    """Fetch and parse the deployed app's `/__manifest` endpoint.

    Returns the JSON body as a dict, expected to contain at least:
        spec, manifest_root_hash, file_count, commit_sha, built_at, version

    Raises ServerManifestError on network/HTTP/JSON errors. Three
    retries with linear backoff (5s, 10s, 15s) on transient failures
    per Process SoT P9.
    """
    import json
    import time

    import httpx

    url = f"{server_url.rstrip('/')}{manifest_path}"
    headers = {
        "X-Symverify-Token": token,
        "Accept": "application/json",
    }

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0, 5, 10, 15]):
        if delay:
            time.sleep(delay)
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last_exc = e
            continue
        if resp.status_code == 401:
            raise ServerManifestError(
                f"401 unauthorized at {url} — check SYMVERIFY_TOKEN"
            )
        if resp.status_code == 503:
            raise ServerManifestError(
                f"503 from {url} — SYMVERIFY_TOKEN unset on server"
            )
        if 500 <= resp.status_code < 600:
            last_exc = ServerManifestError(f"{resp.status_code} from {url}")
            continue  # transient, retry
        if resp.status_code != 200:
            raise ServerManifestError(
                f"unexpected {resp.status_code} from {url}: "
                f"{resp.text[:200]}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise ServerManifestError(
                f"invalid JSON at {url}: {e}"
            ) from e
    raise ServerManifestError(
        f"failed after retries: {last_exc}"
    ) from last_exc
