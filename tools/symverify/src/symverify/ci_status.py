"""GitHub Actions run status — feeds the GUI's CI panel.

Polls `/repos/<owner>/<name>/actions/runs` for the configured repos
and reports each one's most-relevant run (in-progress if any, else
most recent completed). Cached in memory for POLL_INTERVAL_S so the
GUI's 5s refresh doesn't hammer the API.

═════════════════════════════════════════════════════════════════════
CLAUDE ORIENTATION
═════════════════════════════════════════════════════════════════════
What this exists for:
  After a `git push`, the trinity goes briefly drift_expected for
  ~2 min while GHA builds + Watchtower rolls. Without visibility,
  yellow trinity is ambiguous ("is the build running? failed?
  queued?"). This module gives the GUI a "build #42 running, 1m 23s
  elapsed" line so the operator stops guessing.

Where it fits:
  ┌──────────┐         ┌────────────────────┐
  │ GUI loop │ ── 5s ─→│ ci_status.snapshot │  reads in-memory cache
  └──────────┘         └────────────────────┘
                          │ cache miss / stale
                          ↓
                       ┌─────────────┐
                       │ GitHub REST │  /repos/<o>/<r>/actions/runs
                       └─────────────┘

Token:
  Read once from `~/.symmetism/secrets/github.token`. If missing,
  every call returns []. The GUI then just hides the CI panel —
  no error spam.

Repo discovery:
  We look at `~/.symmetism/config/repos.toml` and parse owner/repo
  out of the `remote = "..."` URL. Each repo with a recognized
  GitHub remote becomes a row in the panel.

Don't:
  - Don't poll on every GUI refresh — the cache is here for a reason.
  - Don't store the token in memory longer than needed (re-read
    each refresh) so a runtime token rotation is picked up cleanly.
  - Don't fail loud if the API is slow / unreachable; the daemon
    is the source of truth, not GHA.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from symverify import config


POLL_INTERVAL_S = 15  # cache TTL; min seconds between API calls per repo
RUNS_PER_REPO = 5     # fetch only enough to find the latest in-progress + completed


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One row in the CI panel."""

    owner: str
    repo: str
    workflow_name: str
    status: str           # 'queued' | 'in_progress' | 'completed'
    conclusion: str       # 'success' | 'failure' | 'cancelled' | '' (still running)
    head_sha: str
    head_short: str
    created_at: str       # ISO-8601 UTC
    updated_at: str
    html_url: str
    elapsed_sec: int      # in_progress: now - created; completed: updated - created

    @property
    def is_running(self) -> bool:
        return self.status in ("queued", "in_progress")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)"
)


def _parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """`git@github.com:Symmetism/Platform.git` → ('Symmetism', 'Platform')."""
    m = _GITHUB_REMOTE_RE.search(remote_url)
    return (m["owner"], m["repo"]) if m else None


def _read_token() -> str | None:
    """Lazy: re-read on each call so token rotation works without restart."""
    p = config.secrets_dir() / "github.token"
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip() or None


def _iso_to_epoch(s: str) -> int:
    # GitHub returns "2026-05-07T05:36:46Z"
    from datetime import datetime, timezone

    try:
        return int(
            datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return 0


def _build_run(owner: str, repo: str, raw: dict[str, Any]) -> RunInfo:
    head_sha = raw.get("head_sha") or ""
    created = raw.get("created_at") or ""
    updated = raw.get("updated_at") or ""
    now = int(time.time())
    if raw.get("status") in ("queued", "in_progress"):
        elapsed = max(0, now - _iso_to_epoch(created))
    else:
        elapsed = max(0, _iso_to_epoch(updated) - _iso_to_epoch(created))
    return RunInfo(
        owner=owner,
        repo=repo,
        workflow_name=raw.get("name") or "",
        status=raw.get("status") or "",
        conclusion=raw.get("conclusion") or "",
        head_sha=head_sha,
        head_short=head_sha[:8] if head_sha else "",
        created_at=created,
        updated_at=updated,
        html_url=raw.get("html_url") or "",
        elapsed_sec=elapsed,
    )


# ---------------------------------------------------------------------------
# Public: snapshot()
# ---------------------------------------------------------------------------


class _Cache:
    """Module-level cache. Per (owner, repo): (fetched_at_epoch, list[RunInfo])."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[float, list[RunInfo]]] = {}

    def get(self, key: tuple[str, str]) -> list[RunInfo] | None:
        item = self._data.get(key)
        if item is None:
            return None
        fetched_at, runs = item
        if (time.monotonic() - fetched_at) > POLL_INTERVAL_S:
            return None
        return runs

    def put(self, key: tuple[str, str], runs: list[RunInfo]) -> None:
        self._data[key] = (time.monotonic(), runs)


_CACHE = _Cache()


def snapshot(*, only_relevant: bool = True) -> list[RunInfo]:
    """Return the most-relevant CI runs across all configured GitHub repos.

    `only_relevant=True` returns at most one row per repo: the
    currently-running run if any, else the most recent completed.
    Set False to get the full list (up to RUNS_PER_REPO each).

    Best-effort: errors are swallowed, returns [] if no token, no
    configured repos, or the API is unreachable.
    """
    token = _read_token()
    if not token:
        return []

    repos = config.load_repos()
    if not repos:
        return []

    rows: list[RunInfo] = []
    for repo_cfg in repos.values():
        parsed = _parse_owner_repo(repo_cfg.remote)
        if parsed is None:
            continue
        runs = _fetch_runs(parsed[0], parsed[1], token)
        if not runs:
            continue
        if only_relevant:
            running = next((r for r in runs if r.is_running), None)
            chosen = running or runs[0]
            rows.append(chosen)
        else:
            rows.extend(runs)
    return rows


def _fetch_runs(owner: str, repo: str, token: str) -> list[RunInfo]:
    """Cached fetch. Defers httpx import so test environments without
    network deps still work."""
    key = (owner, repo)
    cached = _CACHE.get(key)
    if cached is not None:
        # Still update elapsed_sec for in-progress rows so the timer
        # advances even between cache misses.
        return [_advance_elapsed(r) for r in cached]
    try:
        import httpx
    except ImportError:
        return []
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    try:
        resp = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"per_page": RUNS_PER_REPO},
            timeout=8.0,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    runs = [_build_run(owner, repo, r) for r in data.get("workflow_runs", [])]
    _CACHE.put(key, runs)
    return runs


def _advance_elapsed(run: RunInfo) -> RunInfo:
    """For in-progress runs, recompute elapsed against current time
    so the GUI shows a ticking timer between API polls."""
    if not run.is_running or not run.created_at:
        return run
    now = int(time.time())
    elapsed = max(0, now - _iso_to_epoch(run.created_at))
    return RunInfo(
        owner=run.owner,
        repo=run.repo,
        workflow_name=run.workflow_name,
        status=run.status,
        conclusion=run.conclusion,
        head_sha=run.head_sha,
        head_short=run.head_short,
        created_at=run.created_at,
        updated_at=run.updated_at,
        html_url=run.html_url,
        elapsed_sec=elapsed,
    )


def humanize_duration(sec: int) -> str:
    """seconds → '12s', '2m 31s', '1h 4m', etc."""
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"
