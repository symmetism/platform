"""Git helpers — thin wrappers over `git` subprocess calls.

Used by `manifest.compute_local_manifest` and `compute_git_manifest`.
We delegate ignore-pattern handling to git itself
(`git ls-files --cached --others --exclude-standard`) so we don't have
to reimplement .gitignore semantics.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git invocation exits non-zero."""


def run_git(repo_path: str | Path, *args: str, **kwargs) -> str:
    """Run `git -C <repo> <args>` and return stdout (text).

    Raises GitError on non-zero exit. `kwargs` is forwarded to subprocess.run
    (e.g. `input=` for stdin); we always set check=False to inspect stderr.
    """
    cmd = ["git", "-C", str(repo_path), *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=False, check=False, **kwargs
    )
    if proc.returncode != 0:
        raise GitError(
            f"{' '.join(cmd)} -> exit {proc.returncode}\n"
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def run_git_bytes(repo_path: str | Path, *args: str) -> bytes:
    """Run git and return raw stdout bytes (for blob content)."""
    cmd = ["git", "-C", str(repo_path), *args]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError(
            f"{' '.join(cmd)} -> exit {proc.returncode}\n"
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')}"
        )
    return proc.stdout


def git_root(repo_path: str | Path) -> str:
    """Absolute path of the repo root, normalized to forward slashes."""
    out = run_git(repo_path, "rev-parse", "--show-toplevel").strip()
    return out.replace("\\", "/")


def git_head_sha(repo_path: str | Path, ref: str = "HEAD") -> str:
    """40-char commit SHA for `ref` (default HEAD)."""
    return run_git(repo_path, "rev-parse", ref).strip()


def git_ls_files_local(repo_path: str | Path) -> list[str]:
    """List working-tree files visible to the local manifest:
    tracked + untracked-but-not-ignored. -z separator handles odd paths."""
    out = run_git(
        repo_path,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    if not out:
        return []
    # Split on NUL; final element is empty.
    parts = out.split("\x00")
    return [p for p in parts if p]


def git_ls_tree(
    repo_path: str | Path, ref: str = "HEAD"
) -> list[tuple[int, str, str]]:
    """Return [(mode_int, blob_sha, path), ...] from `git ls-tree -r <ref>`.

    Only blob entries (skips submodules / commit type entries).
    """
    out = run_git(repo_path, "ls-tree", "-rz", ref)
    if not out:
        return []
    entries: list[tuple[int, str, str]] = []
    for record in out.split("\x00"):
        if not record:
            continue
        # Format: "<mode> <type> <sha>\t<path>"
        head, _, path = record.partition("\t")
        mode_str, type_str, sha = head.split(" ")
        if type_str != "blob":
            continue
        entries.append((int(mode_str, 8), sha, path))
    return entries


def git_cat_file_blob(repo_path: str | Path, blob_sha: str) -> bytes:
    """Raw bytes of a git blob."""
    return run_git_bytes(repo_path, "cat-file", "blob", blob_sha)


def git_ls_files_stage(
    repo_path: str | Path,
) -> dict[str, int]:
    """Return {path: mode_int} for tracked files (from the index).

    Used to give untracked-but-included files a sensible default mode and
    to know whether a working-tree path is tracked.
    """
    out = run_git(repo_path, "ls-files", "-sz")
    result: dict[str, int] = {}
    if not out:
        return result
    for record in out.split("\x00"):
        if not record:
            continue
        head, _, path = record.partition("\t")
        # Format: "<mode> <sha> <stage>"
        mode_str = head.split(" ", 1)[0]
        result[path] = int(mode_str, 8)
    return result
