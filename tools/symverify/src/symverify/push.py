"""`sym push <repo>/<scope> -m "msg"` — Process SoT P5 implementation.

The full P5 pipeline is:
  1. Pre-gate    — anchor verify, secret scan, test run, bracket recompute
  2. Stage       — git add <scope>
  3. Commit      — git commit -m <msg>
  4. Push        — git push origin <branch>
  5. (optional)  Wait for GHA build + Watchtower roll (--watch)
  6. (optional)  Recompute trinity, confirm Q_trinity_X = 0 (--watch)
  7. (optional)  Publish system fold via attestation service (--attest)

We keep steps 1–4 as the core; --watch and --attest are opt-in flags
since they extend wall-clock time substantially. The deploy pipeline
already converges deterministically without operator polling — this
command is ergonomics + visibility, not a coherence requirement.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from symverify import config, git_ops


class PushError(RuntimeError):
    """Raised on any unrecoverable push pipeline failure."""


# ---------------------------------------------------------------------------
# Pre-gate primitives
# ---------------------------------------------------------------------------

# Same regex set used by Q_secrets in stabilizer.py — duplicated here so
# push pre-gate doesn't bring up the full registry just to scan staged
# diff. Keep in sync if either side changes.
_SECRET_PATTERNS = [
    re.compile(r"\bgh" + r"p_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgh" + r"o_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN[ A-Z]+PRIVATE KEY-----"),
    re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-(proj|live|test)?-?[A-Za-z0-9]{20,}\b"),
]


@dataclass(slots=True)
class PreGateResult:
    """Outcome of one pre-gate step."""

    ok: bool
    name: str
    detail: str = ""


def _run(cmd: list[str], cwd: Path | None = None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run wrapper with the same DEVNULL/CREATE_NO_WINDOW
    treatment git_ops uses (so push works under pythonw too)."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kw.setdefault("stdin", subprocess.DEVNULL)
    if sys.platform == "win32":
        kw.setdefault("creationflags", creationflags)
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, **kw
    )


def scan_diff_for_secrets(repo: Path, scope: str | None) -> PreGateResult:
    """Run `git diff --cached` within scope, scan with the Q_secrets regexes.

    Returns ok=False if any pattern matches. Note: this only catches
    *staged* changes; whole-repo scans are Q_secrets' job at audit time.
    """
    args = ["git", "-C", str(repo), "diff", "--cached", "--no-color"]
    if scope:
        args += ["--", scope]
    res = _run(args)
    if res.returncode != 0:
        return PreGateResult(ok=False, name="secret-scan",
                              detail=f"git diff failed: {res.stderr.strip()}")
    diff = res.stdout
    for pat in _SECRET_PATTERNS:
        m = pat.search(diff)
        if m:
            return PreGateResult(
                ok=False, name="secret-scan",
                detail=f"matched {pat.pattern[:40]}: {m.group(0)[:24]}…",
            )
    return PreGateResult(ok=True, name="secret-scan",
                          detail=f"no secret patterns in staged diff ({len(diff)}B)")


def run_anchor_verify(repo: Path) -> PreGateResult:
    """Re-hash policy:immutable anchors. Returns first drift if any."""
    from symverify.canonical import verify_canonical

    try:
        checks = verify_canonical(repo)
    except FileNotFoundError as e:
        return PreGateResult(ok=True, name="anchor-verify",
                              detail=f"no MANIFEST_CANONICAL.json ({e})")
    if not checks:
        return PreGateResult(ok=True, name="anchor-verify",
                              detail="no immutable anchors")
    bad = [c for c in checks if not c.ok]
    if bad:
        first = bad[0]
        return PreGateResult(
            ok=False, name="anchor-verify",
            detail=f"{first.path}: expected {first.expected_sha256[:16]}…, "
                   f"got {(first.got_sha256 or '?')[:16]}…",
        )
    return PreGateResult(ok=True, name="anchor-verify",
                          detail=f"{len(checks)} anchor(s) verified")


def run_pytest(repo: Path) -> PreGateResult:
    """Best-effort pytest run. Skips if no tests/ or pyproject in scope.

    We invoke `python -m pytest` from the repo root with -q, capturing
    output. A non-zero exit halts the push.
    """
    if not (repo / "pyproject.toml").is_file() and not list(repo.glob("**/tests")):
        return PreGateResult(ok=True, name="pytest", detail="no tests in repo")
    res = _run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-x"],
        cwd=repo,
        timeout=300,
    )
    if res.returncode == 0:
        last = res.stdout.strip().splitlines()[-1] if res.stdout else ""
        return PreGateResult(ok=True, name="pytest", detail=last[:80])
    if res.returncode == 5:
        # pytest exit 5 = "no tests collected" — not a failure for us.
        return PreGateResult(ok=True, name="pytest", detail="no tests collected")
    failure_lines = (res.stdout + "\n" + res.stderr).strip().splitlines()
    tail = " | ".join(failure_lines[-3:])[:200]
    return PreGateResult(ok=False, name="pytest", detail=f"exit {res.returncode}: {tail}")


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def stage_changes(repo: Path, scope: str | None) -> int:
    """git add <scope>. Returns count of changed paths now staged."""
    args = ["git", "-C", str(repo), "add"]
    args.append(scope if scope else "-A")
    res = _run(args)
    if res.returncode != 0:
        raise PushError(f"git add failed: {res.stderr.strip()}")
    # Count staged paths.
    res = _run(["git", "-C", str(repo), "diff", "--cached", "--name-only"])
    if res.returncode != 0:
        return 0
    return len([line for line in res.stdout.splitlines() if line])


def commit(repo: Path, message: str, sign: bool = False) -> str:
    """git commit -m <message>. Returns the new commit's short SHA.

    Empty commits are rejected by git itself; we let that error surface.
    """
    args = ["git", "-C", str(repo), "commit", "-m", message]
    if sign:
        args.append("-S")
    res = _run(args)
    if res.returncode != 0:
        raise PushError(
            f"git commit failed: {(res.stderr or res.stdout).strip()}"
        )
    return git_ops.git_head_sha(repo)[:12]


def push(repo: Path, branch: str | None = None) -> str:
    """git push origin <branch>. Returns the remote URL pushed to."""
    if branch is None:
        res = _run(["git", "-C", str(repo), "branch", "--show-current"])
        if res.returncode != 0 or not res.stdout.strip():
            raise PushError("could not resolve current branch")
        branch = res.stdout.strip()
    res = _run(["git", "-C", str(repo), "push", "origin", branch])
    if res.returncode != 0:
        raise PushError(f"git push failed: {res.stderr.strip() or res.stdout.strip()}")
    # Read remote URL for display.
    res2 = _run(["git", "-C", str(repo), "remote", "get-url", "origin"])
    return res2.stdout.strip() if res2.returncode == 0 else "origin"


# ---------------------------------------------------------------------------
# Optional --watch step: poll the deployed server for commit-SHA convergence
# ---------------------------------------------------------------------------


def watch_for_convergence(
    repo_name: str,
    expected_sha: str,
    *,
    timeout_sec: int = 600,
    poll_sec: int = 10,
    on_tick=None,
) -> bool:
    """Poll all configured servers for `repo_name` until /__manifest reports
    the new commit SHA, or timeout. Returns True on success.

    `on_tick(elapsed, latest_sha)` is invoked after every poll for the
    caller to render progress.
    """
    import httpx
    from symverify import manifest as _manifest

    servers = config.load_servers()
    matching = [
        sc for sc in servers.values() if sc.repo == repo_name and sc.token_file.is_file()
    ]
    if not matching:
        if on_tick:
            on_tick(0, f"no servers configured for repo '{repo_name}'")
        return False

    expected_short = expected_sha[:12]
    deadline = time.monotonic() + timeout_sec
    last = ""
    while time.monotonic() < deadline:
        all_match = True
        for sc in matching:
            try:
                token = sc.token_file.read_text(encoding="utf-8").strip()
                body = _manifest.compute_server_manifest(
                    sc.url, token, manifest_path=sc.manifest_path
                )
                last = body.get("commit_sha") or ""
                if not last.startswith(expected_short[:8]):
                    all_match = False
                    break
            except (httpx.HTTPError, _manifest.ServerManifestError) as e:
                last = f"(transient: {e})"
                all_match = False
                break
        elapsed = int(time.monotonic() - (deadline - timeout_sec))
        if on_tick:
            on_tick(elapsed, last)
        if all_match:
            return True
        time.sleep(poll_sec)
    return False


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PushResult:
    """Aggregated outcome of one `sym push` invocation."""

    repo_name: str
    repo_path: Path
    scope: str | None
    pre_gates: list[PreGateResult]
    staged_count: int = 0
    short_sha: str = ""
    pushed_to: str = ""
    converged: bool | None = None
    converged_after_sec: int = 0
    attested_fold: str | None = None
    attestation_url: str = ""

    def alarm(self) -> bool:
        return any(not g.ok for g in self.pre_gates)


def resolve_repo_scope(arg: str) -> tuple[str, str | None]:
    """Parse `<repo>/<scope>` into (repo, scope) or ('repo', None)."""
    if "/" in arg:
        repo, scope = arg.split("/", 1)
        return repo, scope or None
    return arg, None


def run_push(
    target: str,
    message: str,
    *,
    skip_tests: bool = False,
    skip_anchor: bool = False,
    skip_secret_scan: bool = False,
    sign: bool = False,
    on_event=None,
) -> PushResult:
    """Run the full push pipeline. Raises PushError on hard failures.

    `on_event(stage: str, detail: str)` is called as each stage starts /
    completes so the CLI can render progress.
    """
    repo_name, scope = resolve_repo_scope(target)
    repos = config.load_repos()
    if repo_name not in repos:
        raise PushError(
            f"repo '{repo_name}' not configured in {config.config_dir()}/repos.toml; "
            f"have: {sorted(repos)}"
        )
    rc = repos[repo_name]
    if not rc.path.is_dir():
        raise PushError(f"repo path missing: {rc.path}")

    def emit(stage: str, detail: str = "") -> None:
        if on_event:
            on_event(stage, detail)

    pre_gates: list[PreGateResult] = []

    if not skip_anchor:
        emit("anchor-verify", "starting")
        r = run_anchor_verify(rc.path)
        pre_gates.append(r)
        if not r.ok:
            return PushResult(repo_name=repo_name, repo_path=rc.path,
                              scope=scope, pre_gates=pre_gates)

    emit("stage", f"git add {scope or '-A'}")
    staged = stage_changes(rc.path, scope)
    if staged == 0:
        # Run secret/pytest gates anyway? No — nothing to commit, exit clean.
        return PushResult(
            repo_name=repo_name, repo_path=rc.path, scope=scope,
            pre_gates=pre_gates, staged_count=0,
        )

    if not skip_secret_scan:
        emit("secret-scan", "starting")
        r = scan_diff_for_secrets(rc.path, scope)
        pre_gates.append(r)
        if not r.ok:
            # Unstage so operator's worktree isn't left in a half-committed state.
            _run(["git", "-C", str(rc.path), "reset"], cwd=rc.path)
            return PushResult(repo_name=repo_name, repo_path=rc.path,
                              scope=scope, pre_gates=pre_gates,
                              staged_count=staged)

    if not skip_tests:
        emit("pytest", "starting")
        r = run_pytest(rc.path)
        pre_gates.append(r)
        if not r.ok:
            _run(["git", "-C", str(rc.path), "reset"], cwd=rc.path)
            return PushResult(repo_name=repo_name, repo_path=rc.path,
                              scope=scope, pre_gates=pre_gates,
                              staged_count=staged)

    emit("commit", message[:80])
    short_sha = commit(rc.path, message, sign=sign)

    emit("push", "to origin")
    pushed_to = push(rc.path)

    return PushResult(
        repo_name=repo_name, repo_path=rc.path, scope=scope,
        pre_gates=pre_gates, staged_count=staged,
        short_sha=short_sha, pushed_to=pushed_to,
    )
