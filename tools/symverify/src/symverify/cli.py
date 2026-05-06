"""SymVerify CLI (D7).

Subcommands:
  sym status [--explain]       Full audit + rings + bracket table
  sym verify-canonical         Re-hash all immutable anchors
  sym registry list            Print active Q_A registry
  sym init                     First-time setup (registers repos, runs audit)

`--explain` triggers a Haiku/OpenAI narrative on the current snapshot
(deferred to E5 — for now it just prints a hint).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from symverify import db, narrative, state_collect


# Windows defaults stdout to cp1252 which can't encode the Unicode
# glyphs (✓, ✗, ◉, ≠) we render. Force UTF-8 for both stdout/stderr at
# import time. No-op on platforms whose default is already utf-8.
def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()

from symverify import __version__, config, fingerprint as fp, manifest, render
from symverify.canonical import LockdownError, verify_canonical
from symverify.stabilizer import (
    Registry,
    State,
    STATUS_CONSERVED,
    STATUS_DRIFT_ALARM,
    STATUS_DRIFT_EXPECTED,
    STATUS_PENDING,
)


@click.group()
@click.version_option(__version__, prog_name="sym")
def main() -> None:
    """sym — the Symmetism verifier and stabilizer-audit CLI.

    See `_command/06_BATTLE_PLAN.md` for the full spec.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# `_build_state` and `_collect_invariants` moved to symverify.state_collect
# at J1 so the daemon can share them. Local aliases kept for clarity.
_collect_invariants = state_collect.collect_invariants
_build_state = state_collect.build_state


# ---------------------------------------------------------------------------
# `sym status`
# ---------------------------------------------------------------------------


@main.command()
@click.option("--explain", is_flag=True, help="Generate a narrative summary (Phase E).")
def status(explain: bool) -> None:
    """Run the full Stabilizer audit; render rings + bracket table."""
    repos = config.load_repos()
    if not repos:
        click.echo(
            "no repos in ~/.symmetism/config/repos.toml. Run `sym init` first."
        )
        raise SystemExit(2)

    cmd_dir = config.command_dir()
    registry_path = cmd_dir / "STABILIZER_REGISTRY.json"
    if not registry_path.is_file():
        click.echo(f"no registry at {registry_path}", err=True)
        raise SystemExit(2)

    registry = Registry.load(registry_path)
    servers = config.load_servers()
    state, meta = _build_state(repos, servers)
    report = registry.audit(state)

    # --- compose render --------------------------------------------------
    console = Console()

    rings = render.render_rings(
        local=state.reflexivity_local_hash,
        git=state.reflexivity_git_hash,
        server=state.reflexivity_server_hash,
        alarm=report.alarm,
    )
    repo_rows = []
    for name, m in meta.items():
        if "error" in m:
            from rich.text import Text

            repo_rows.append(Text(f"{name:14s}{m['error']}", style=render.ALARM))
        else:
            repo_rows.append(
                render.render_repo_row(
                    name.capitalize(),
                    m.get("local"),
                    m.get("git"),
                    m.get("server"),
                    short_sha=m.get("short_sha"),
                )
            )

    # G1: system fold over both repos' trinities + cross-repo invariants.
    trinities = {
        name: m.get("trinity", "")
        for name, m in meta.items()
        if m.get("trinity")
    }
    # Merge invariants — they should agree across repos when Q_cross_repo
    # is conserved; if they don't, _trinity_bracket on Q_cross_repo will
    # already have flagged it.
    fold_invariants: dict[str, str] = {}
    for inv in (state.invariants_R, state.invariants_P):
        for k, v in inv.items():
            fold_invariants.setdefault(k, v)
    if trinities:
        fingerprint = fp.system_fold(trinities, fold_invariants, __version__)
    else:
        fingerprint = "<no-fold>"

    panel = render.render_full_status(
        fingerprint=fingerprint,
        rings=rings,
        repo_rows=repo_rows,
        audit_header=render.render_audit_header(report),
        audit_table=render.render_audit_table(report),
        timestamp_iso=report.audit_run_at,
    )
    console.print(panel)

    # --- write BRACKETS.json --------------------------------------------
    brackets_path = cmd_dir / "BRACKETS.json"
    if brackets_path.parent.is_dir():
        import json

        existing = (
            json.loads(brackets_path.read_text(encoding="utf-8"))
            if brackets_path.is_file()
            else {}
        )
        existing.update(report.to_json())
        existing["comment"] = (
            existing.get("comment", "") + "\n" + f"Last updated by `sym status` at {report.audit_run_at}."
        ).strip()
        brackets_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # --- E2: persist snapshot to SQLite ---------------------------------
    snapshot_status = "lockdown" if report.alarm else (
        "drift"
        if any(b.status not in (STATUS_CONSERVED, STATUS_PENDING) for b in report.brackets)
        else "clean"
    )
    snapshot_id: int | None = None
    try:
        with db.connect() as conn:
            snapshot_id = db.insert_snapshot(
                conn,
                trinity_r=meta.get("reflexivity", {}).get("trinity"),
                trinity_p=meta.get("platform", {}).get("trinity"),
                system_fold=None,  # populated at G1
                brackets={b.charge_id: b.to_json() for b in report.brackets},
                status=snapshot_status,
            )
            db.insert_event(
                conn,
                kind="manual" if not explain else "explain",
                repo=None,
                detail={"alarm": report.alarm, "snapshot_id": snapshot_id},
            )
    except Exception as e:
        console.print(f"[{render.DRIFT}](db write skipped: {e})[/]")

    # --- E5: --explain triggers narrative -------------------------------
    if explain and snapshot_id is not None:
        snap_for_narr = {
            "status": snapshot_status,
            "trinity_r": meta.get("reflexivity", {}).get("trinity"),
            "trinity_p": meta.get("platform", {}).get("trinity"),
            "system_fold": None,
            "brackets": {b.charge_id: b.to_json() for b in report.brackets},
        }
        text = narrative.narrate(snap_for_narr, trigger="manual")
        if text is None:
            text = narrative.unavailable_text("OpenAI client unavailable or API error")
        try:
            with db.connect() as conn:
                db.insert_narrative(
                    conn,
                    trigger="manual",
                    text=text,
                    snapshot_id=snapshot_id,
                )
        except Exception:
            pass
        console.print(f"\n[{render.MUTED}]narrative:[/]")
        console.print(text)

    if report.alarm:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# `sym verify-canonical`
# ---------------------------------------------------------------------------


@main.command("verify-canonical")
def verify_canonical_cmd() -> None:
    """Re-hash every `policy:immutable` anchor in every configured repo."""
    repos = config.load_repos()
    if not repos:
        click.echo("no repos configured", err=True)
        raise SystemExit(2)
    console = Console()
    any_drift = False
    for name, rc in repos.items():
        try:
            checks = verify_canonical(rc.path)
        except FileNotFoundError as e:
            console.print(f"[{render.MUTED}]{name}: {e}[/]")
            continue
        if not checks:
            console.print(f"[{render.MUTED}]{name}: no immutable anchors[/]")
            continue
        for c in checks:
            if c.ok:
                console.print(f"[{render.STABLE}]✓[/] {name}/{c.path}  sha256={c.expected_sha256[:16]}…")
            else:
                any_drift = True
                console.print(f"[{render.ALARM}]✗[/] {name}/{c.path}")
                if c.expected_sha256 and c.got_sha256:
                    console.print(f"   expected: {c.expected_sha256}")
                    console.print(f"   got:      {c.got_sha256}")
                elif c.error:
                    console.print(f"   error: {c.error}")
    if any_drift:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# `sym registry list`
# ---------------------------------------------------------------------------


@main.group()
def registry() -> None:
    """Manage the Stabilizer Q_A registry."""


@registry.command("list")
def registry_list() -> None:
    """Print the active Q_A registry from `_command/STABILIZER_REGISTRY.json`."""
    cmd_dir = config.command_dir()
    p = cmd_dir / "STABILIZER_REGISTRY.json"
    if not p.is_file():
        click.echo(f"no registry at {p}", err=True)
        raise SystemExit(2)
    reg = Registry.load(p)
    console = Console()
    console.print(
        f"[bold {render.STABLE}]STABILIZER_REGISTRY[/]  spec={reg.spec_version}  "
        f"DIM_STAB={reg.framework_dim_stab}  charges={len(reg.charges)}"
    )
    for c in reg.charges:
        console.print(f"  [{render.STABLE}]Q_{c.ordinal}[/]  {c.id}")
        console.print(f"     [{render.MUTED}]{c.description}[/]")
        ev = (
            c.expected_value
            if c.expected_value and len(str(c.expected_value)) <= 40
            else f"{str(c.expected_value)[:32]}…" if c.expected_value else "(unset)"
        )
        console.print(
            f"     [{render.MUTED}]expected:[/] {ev}    "
            f"[{render.MUTED}]alarm_on_nonzero:[/] {c.alarm_on_nonzero}"
        )


# ---------------------------------------------------------------------------
# `sym init`
# ---------------------------------------------------------------------------


@main.command()
def init() -> None:
    """Verify config + registry are present and runnable. (First-time setup.)

    For v0.1 this is mostly a sanity check: it doesn't write the
    repos.toml (operator's responsibility per A6) but does run the
    first audit so BRACKETS.json gets populated with computed values.
    """
    console = Console()
    repos = config.load_repos()
    if not repos:
        console.print(
            f"[{render.ALARM}]no repos in {config.config_dir()}/repos.toml — see _command/06_BATTLE_PLAN.md A6[/]"
        )
        raise SystemExit(2)
    console.print(f"[{render.STABLE}]✓[/] {len(repos)} repo(s) configured: {', '.join(repos)}")

    cmd_dir = config.command_dir()
    if not (cmd_dir / "STABILIZER_REGISTRY.json").is_file():
        console.print(
            f"[{render.ALARM}]no registry at {cmd_dir}/STABILIZER_REGISTRY.json[/]"
        )
        raise SystemExit(2)
    console.print(f"[{render.STABLE}]✓[/] registry present at {cmd_dir}/STABILIZER_REGISTRY.json")

    if not config.state_dir().is_dir():
        config.state_dir().mkdir(parents=True, exist_ok=True)
        console.print(f"[{render.STABLE}]✓[/] created state dir {config.state_dir()}")

    console.print(f"[{render.MUTED}]running first audit via `sym status`...[/]")
    # Defer to status (the command above) by invoking it programmatically.
    ctx = click.get_current_context()
    ctx.invoke(status, explain=False)


# ---------------------------------------------------------------------------
# `sym daemon` + service install/uninstall (Phase J)
# ---------------------------------------------------------------------------


@main.command("daemon")
@click.option("--log-level", default="INFO", show_default=True,
              help="logging.{DEBUG,INFO,WARNING,ERROR}")
@click.option("--log-file", default=None,
              help="Path to log file. Defaults to ~/.symmetism/state/daemon.log "
                   "when stdout is unattached (pythonw.exe), else stdout.")
def daemon_cmd(log_level: str, log_file: str | None) -> None:
    """Run the SymVerify daemon (filesystem + wake + hourly triggers).

    On Windows the Scheduled Task launches us via pythonw.exe (no
    console). pythonw's sys.stdout / sys.stderr are None — Python's
    logging StreamHandler then fails on first write and the worker
    thread silently dies. Detect that and route logging to a rotating
    file under ~/.symmetism/state/daemon.log instead.
    """
    import logging as _logging
    import logging.handlers as _logh
    import sys as _sys
    from pathlib import Path as _Path
    from symverify import config as _config
    from symverify import daemon as _daemon

    level = getattr(_logging, log_level.upper(), _logging.INFO)
    fmt = _logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = _logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    is_pythonw = _sys.executable.lower().endswith("pythonw.exe")
    needs_file = log_file is not None or _sys.stdout is None or is_pythonw

    if needs_file:
        if log_file is None:
            log_file = str(_config.state_dir() / "daemon.log")
        path = _Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Reroute sys.stdout/stderr to the log file too, so any
        # stray print() or unhandled-exception traceback also lands
        # in the file (rather than crashing on a None stdout under
        # pythonw, which silently kills threads).
        try:
            stream = open(path, "a", encoding="utf-8", buffering=1)
            _sys.stdout = stream
            _sys.stderr = stream
        except Exception:
            pass

        fh = _logh.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    else:
        sh = _logging.StreamHandler(_sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    d = _daemon.Daemon()
    d.run()


@main.command("install-service")
@click.option(
    "--task-name", default=None,
    help="Override the Scheduled Task name (default: 'Symmetism SymVerify Daemon').",
)
def install_service_cmd(task_name: str | None) -> None:
    """Create a Windows Scheduled Task that runs `sym daemon` at logon.

    Idempotent: re-running replaces the existing task. Linux/macOS: the
    daemon can be invoked via systemd / launchd manually; this command
    is Windows-only for v1.
    """
    if sys.platform != "win32":
        click.echo("install-service is Windows-only for v1", err=True)
        raise SystemExit(2)
    from symverify import daemon as _daemon

    name = task_name or _daemon.DEFAULT_TASK_NAME
    try:
        _daemon.install_windows_service(name)
    except Exception as e:
        click.echo(f"[error] {e}", err=True)
        raise SystemExit(1)
    click.echo(
        f"installed Scheduled Task '{name}' — runs at every user logon, "
        "unprivileged, no console window."
    )


@main.command("uninstall-service")
@click.option("--task-name", default=None)
def uninstall_service_cmd(task_name: str | None) -> None:
    """Remove the Scheduled Task created by `sym install-service`."""
    if sys.platform != "win32":
        click.echo("uninstall-service is Windows-only for v1", err=True)
        raise SystemExit(2)
    from symverify import daemon as _daemon

    name = task_name or _daemon.DEFAULT_TASK_NAME
    try:
        _daemon.uninstall_windows_service(name)
    except Exception as e:
        click.echo(f"[error] {e}", err=True)
        raise SystemExit(1)
    click.echo(f"removed Scheduled Task '{name}'.")


@main.command("service-status")
@click.option("--task-name", default=None)
def service_status_cmd(task_name: str | None) -> None:
    """Show whether the Scheduled Task is installed + its last run state."""
    if sys.platform != "win32":
        click.echo("service-status is Windows-only for v1", err=True)
        raise SystemExit(2)
    from symverify import daemon as _daemon

    name = task_name or _daemon.DEFAULT_TASK_NAME
    info = _daemon.query_windows_service(name)
    console = Console()
    if info is None:
        console.print(f"[{render.MUTED}](not installed: '{name}')[/]")
        raise SystemExit(1)
    console.print(f"[{render.STABLE}]✓[/] Scheduled Task installed: [{render.STABLE}]{name}[/]")
    for k in ("TaskName", "Status", "Last Run Time", "Next Run Time", "Last Result"):
        if k in info:
            console.print(f"  [{render.MUTED}]{k}:[/] {info[k]}")


# ---------------------------------------------------------------------------
# `sym gui` — Windows GUI app
# ---------------------------------------------------------------------------


@main.command("gui")
def gui_cmd() -> None:
    """Launch the Symmetism Coherence GUI window.

    Reads ~/.symmetism/state/status.json (the daemon writes it) and
    renders the same trinity / brackets / narrative the CLI shows,
    plus buttons for manual audit, narrative explain, the verify
    page, and service install/uninstall.

    The GUI shares the same audit and narrative pipeline as the CLI
    — no duplicate logic. If the daemon isn't running, you'll see
    an empty fold; the GUI's "⚙" panel can install it.
    """
    try:
        from symverify import gui as _gui
    except ImportError as e:
        click.echo(
            f"[error] customtkinter not installed ({e}). "
            "Install with: pip install customtkinter>=5.2",
            err=True,
        )
        raise SystemExit(2)
    _gui.main()


# ---------------------------------------------------------------------------
# `sym push` and `sym scaffold` (F11)
# ---------------------------------------------------------------------------


@main.command("push")
@click.argument("target")
@click.option("-m", "--message", required=True, help="Commit message.")
@click.option("--watch", is_flag=True,
              help="After push, poll the deployed servers' /__manifest until "
                   "the new commit SHA appears (or 10 min timeout).")
@click.option("--attest", is_flag=True,
              help="After --watch converges, invoke `sym attest` to publish "
                   "the new system fold to the public Gist.")
@click.option("--skip-tests", is_flag=True,
              help="Skip the pytest pre-gate (use sparingly).")
@click.option("--skip-anchor", is_flag=True,
              help="Skip MANIFEST_CANONICAL re-hash pre-gate.")
@click.option("--skip-secret-scan", is_flag=True,
              help="Skip the secret regex scan of staged diff.")
@click.option("--sign", is_flag=True, help="GPG-sign the commit.")
def push_cmd(target: str, message: str, watch: bool, attest: bool,
             skip_tests: bool, skip_anchor: bool, skip_secret_scan: bool,
             sign: bool) -> None:
    """sym push <repo>[/<scope>] -m "msg" — staged-commit-push pipeline.

    Runs the Process SoT P5 pre-gates (anchor verify, secret scan,
    pytest), stages, commits, pushes. With --watch it polls the
    deployed server until the new commit SHA appears in /__manifest.
    With --attest (implies --watch) it then publishes the new system
    fold via the attestation service.

    \b
    Examples:
      sym push reflexivity -m "feat: new physics module"
      sym push platform/apps/attestation-service -m "fix: cache TTL"
      sym push reflexivity --watch --attest -m "feat: F12 trinity poll"
    """
    from symverify import push as _push

    console = Console()

    def on_event(stage: str, detail: str) -> None:
        console.print(f"[{render.MUTED}]→[/] {stage:14s}  {detail}")

    try:
        result = _push.run_push(
            target, message,
            skip_tests=skip_tests,
            skip_anchor=skip_anchor,
            skip_secret_scan=skip_secret_scan,
            sign=sign,
            on_event=on_event,
        )
    except _push.PushError as e:
        console.print(f"[{render.ALARM}][error][/] {e}")
        raise SystemExit(1)

    # Render pre-gate results.
    console.print()
    for g in result.pre_gates:
        marker = (
            f"[{render.STABLE}]✓[/]" if g.ok else f"[{render.ALARM}]✗[/]"
        )
        console.print(f"  {marker} {g.name:14s} [{render.MUTED}]{g.detail}[/]")

    if result.alarm():
        console.print(
            f"\n[{render.ALARM}]push aborted: pre-gate failed (changes left "
            f"unstaged in working tree)[/]"
        )
        raise SystemExit(1)

    if result.staged_count == 0:
        console.print(f"\n[{render.MUTED}]nothing to commit ({target!r} clean)[/]")
        return

    console.print(
        f"\n  [{render.STABLE}]✓[/] commit  [{render.STABLE}]{result.short_sha}[/]  "
        f"({result.staged_count} file(s)) → {result.pushed_to}"
    )

    if watch or attest:
        console.print()
        console.print(f"[{render.MUTED}]waiting for server convergence on "
                      f"{result.short_sha}…[/]")

        def on_tick(elapsed: int, latest: str) -> None:
            console.print(
                f"  [{render.MUTED}]+{elapsed:3d}s  server reports {latest}[/]"
            )

        converged = _push.watch_for_convergence(
            result.repo_name, result.short_sha,
            timeout_sec=600, poll_sec=10, on_tick=on_tick,
        )
        if not converged:
            console.print(
                f"[{render.DRIFT}]⚠ timed out waiting for server to roll[/]"
            )
            raise SystemExit(2)
        console.print(f"[{render.STABLE}]✓ servers converged[/]")

        if attest:
            console.print()
            console.print(f"[{render.MUTED}]publishing attestation…[/]")
            ctx = click.get_current_context()
            ctx.invoke(
                attest_cmd,
                service_url="https://symmetism.com",
                token_file=None,
            )


@main.command("scaffold")
@click.argument("target")
@click.option("--force", is_flag=True,
              help="Overwrite existing files in the target dir.")
def scaffold_cmd(target: str, force: bool) -> None:
    """sym scaffold <repo>/<app-name> — generate a Symmetism-tracked app.

    Writes pyproject.toml, Dockerfile, FastAPI main.py, the
    /__manifest helper, README, and a Caddy snippet under
    <repo>/apps/<app-name>/. Idempotent only with --force; refuses
    to clobber existing dirs by default.

    \b
    Examples:
      sym scaffold platform/coherence-dashboard
      sym scaffold reflexivity/lean-verifier --force
    """
    from symverify import scaffold as _scaffold

    console = Console()
    try:
        result = _scaffold.scaffold_app(target, force=force)
    except _scaffold.ScaffoldError as e:
        console.print(f"[{render.ALARM}][error][/] {e}")
        raise SystemExit(1)

    console.print(
        f"[{render.STABLE}]✓[/] scaffolded [{render.STABLE}]{result.app_name}[/] "
        f"in {result.repo_name} at [{render.MUTED}]{result.app_dir}[/]"
    )
    for f in result.files_written:
        rel = f.relative_to(result.app_dir.parent)
        console.print(f"  [{render.MUTED}]{rel}[/]")
    console.print()
    console.print(f"[{render.MUTED}]next steps:[/]")
    console.print(f"  1. Edit src/{result.app_name.replace('-', '_')}/main.py with your routes")
    console.print(f"  2. Add the Caddy snippet from {result.caddy_snippet_path.name} to server/Caddyfile")
    console.print(f"  3. Add the service to server/compose.yaml")
    console.print(f"  4. sym push {result.repo_name}/apps/{result.app_name} -m \"feat: scaffold {result.app_name}\"")


# ---------------------------------------------------------------------------
# `sym attest` (H4)
# ---------------------------------------------------------------------------


@main.command("attest")
@click.option(
    "--service-url",
    default="https://symmetism.com",
    show_default=True,
    help="Base URL of the attestation service.",
)
@click.option(
    "--token-file",
    default=None,
    help="Path to the publish token (default: ~/.symmetism/secrets/attestation.publish.token).",
)
def attest_cmd(service_url: str, token_file: str | None) -> None:
    """Compute the current snapshot and POST it to the attestation
    service for publication to the public Gist (battle plan H4).
    """
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path
    from symverify.canonical import LockdownError  # noqa: F401  (just to import the error class)

    import httpx

    repos = config.load_repos()
    if not repos:
        click.echo("no repos configured", err=True)
        raise SystemExit(2)
    servers = config.load_servers()
    state, meta = _build_state(repos, servers)

    cmd_dir = config.command_dir()
    registry_path = cmd_dir / "STABILIZER_REGISTRY.json"
    if not registry_path.is_file():
        click.echo(f"no registry at {registry_path}", err=True)
        raise SystemExit(2)
    registry = Registry.load(registry_path)
    report = registry.audit(state)

    if report.alarm:
        click.echo(
            "[ALARM] aborting attestation publish — operator must clear lockdown",
            err=True,
        )
        raise SystemExit(1)

    # Compose the attestation payload.
    trinities = {
        name: m.get("trinity", "")
        for name, m in meta.items()
        if m.get("trinity")
    }
    invariants: dict[str, str] = {}
    for inv in (state.invariants_R, state.invariants_P):
        for k, v in inv.items():
            invariants.setdefault(k, v)
    folded = (
        fp.system_fold(trinities, invariants, __version__)
        if trinities
        else None
    )

    attestation = {
        "spec": "symverify-fingerprint/1",
        "system_fold": folded,
        "trinities": trinities,
        "invariants": invariants,
        "version": __version__,
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alarm": report.alarm,
        "drift": any(b.status not in (STATUS_CONSERVED, STATUS_PENDING) for b in report.brackets),
        "brackets": {b.charge_id: b.to_json() for b in report.brackets},
        "repos": {
            name: {
                "commit_sha": m.get("short_sha"),
                "trinity": m.get("trinity"),
                "server_commit_sha": m.get("server_commit_sha"),
                "server_url": m.get("server_url"),
            }
            for name, m in meta.items()
            if not m.get("error")
        },
    }

    # Resolve token.
    if token_file:
        tok_path = Path(token_file).expanduser()
    else:
        tok_path = config.secrets_dir() / "attestation.publish.token"
    if not tok_path.is_file():
        click.echo(
            f"[error] no publish token at {tok_path}\n"
            "  generate one (32-byte hex): openssl rand -hex 32 > "
            f"{tok_path}\n"
            "  then set ATTESTATION_PUBLISH_TOKEN to the same value on the deployed service.",
            err=True,
        )
        raise SystemExit(2)
    token = tok_path.read_text(encoding="utf-8").strip()

    url = f"{service_url.rstrip('/')}/api/publish"
    try:
        resp = httpx.post(
            url,
            headers={"X-Attestation-Token": token},
            json=attestation,
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        click.echo(f"[error] connection failed to {url}: {e}", err=True)
        raise SystemExit(1)
    if resp.status_code != 200:
        click.echo(
            f"[error] {resp.status_code} from {url}: {resp.text[:200]}",
            err=True,
        )
        raise SystemExit(1)

    click.echo(f"published: {folded} at {attestation['verified_at']}")


# ---------------------------------------------------------------------------
# `sym fold` (G1 / G2)
# ---------------------------------------------------------------------------


@main.command("fold")
@click.option(
    "--verify",
    is_flag=True,
    help="Recompute cross-repo invariants and check Q_cross_repo bracket.",
)
def fold_cmd(verify: bool) -> None:
    """Print the system fold — a 16-char Crockford fingerprint over
    both repos' trinities plus cross-repo invariants. Stable when
    everything is aligned; changes on any drift.
    """
    repos = config.load_repos()
    if not repos:
        click.echo("no repos in ~/.symmetism/config/repos.toml", err=True)
        raise SystemExit(2)
    servers = config.load_servers()
    state, meta = _build_state(repos, servers)

    trinities = {
        name: m.get("trinity", "")
        for name, m in meta.items()
        if m.get("trinity")
    }
    invariants: dict[str, str] = {}
    for inv in (state.invariants_R, state.invariants_P):
        for k, v in inv.items():
            invariants.setdefault(k, v)

    if not trinities:
        click.echo("no trinity fingerprints — nothing to fold", err=True)
        raise SystemExit(2)

    folded = fp.system_fold(trinities, invariants, __version__)

    console = Console()
    console.print(f"[bold {render.STABLE}]System fold:[/] [{render.STABLE}]{folded}[/]")
    console.print()
    console.print(f"[{render.MUTED}]composition[/]")
    for name, t in sorted(trinities.items()):
        console.print(f"  {name:14s} trinity = [{render.STABLE}]{t}[/]")
    if invariants:
        console.print(f"  [{render.MUTED}]invariants:[/]")
        for k, v in sorted(invariants.items()):
            shown = v if len(v) <= 32 else f"{v[:30]}…"
            console.print(f"    {k:32s} = [{render.MUTED}]{shown}[/]")
    console.print(f"  symverify_version = [{render.MUTED}]{__version__}[/]")

    if verify:
        cmd_dir = config.command_dir()
        registry_path = cmd_dir / "STABILIZER_REGISTRY.json"
        if not registry_path.is_file():
            console.print(f"[{render.ALARM}]no registry at {registry_path}[/]")
            raise SystemExit(2)
        registry = Registry.load(registry_path)
        report = registry.audit(state)
        cross = next(
            (b for b in report.brackets if b.charge_id == "Q_cross_repo"),
            None,
        )
        if cross is None:
            console.print(f"[{render.ALARM}]Q_cross_repo not in registry[/]")
            raise SystemExit(2)
        marker = (
            f"[{render.STABLE}]✓[/]"
            if cross.status == STATUS_CONSERVED
            else (
                f"[{render.DRIFT}]⚠[/]"
                if cross.status == STATUS_DRIFT_EXPECTED
                else f"[{render.ALARM}]✗[/]"
            )
        )
        console.print()
        console.print(f"  Q_cross_repo  {marker}  [{render.MUTED}]{cross.descriptor}[/]")
        if cross.status == STATUS_DRIFT_ALARM:
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# `sym timeline` (I1)
# ---------------------------------------------------------------------------


@main.command("timeline")
@click.option(
    "--days", default=30, type=int, show_default=True,
    help="How many days of history to render."
)
@click.option(
    "--limit", default=200, type=int, show_default=True,
    help="Max snapshots to read from the DB."
)
def timeline_cmd(days: int, limit: int) -> None:
    """Vertical strip of recent coherence states grouped by day.

    One row per day; glyph counts: ✓ conserved, ⚠ drift, ✗ lockdown.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    console = Console()
    try:
        with db.connect() as conn:
            snapshots = db.list_snapshots(conn, limit=limit)
    except Exception as e:
        console.print(f"[{render.ALARM}]database read failed: {e}[/]")
        raise SystemExit(2)

    if not snapshots:
        console.print(f"[{render.MUTED}]no snapshots yet — run `sym status` to record one[/]")
        return

    # Group by date.
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        day = s["taken_at"][:10]  # YYYY-MM-DD
        by_day[day].append(s)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%d")
    by_day = {d: ss for d, ss in by_day.items() if d >= cutoff}

    if not by_day:
        console.print(
            f"[{render.MUTED}]no snapshots in the last {days} days[/]"
        )
        return

    # Render: newest day first.
    console.print(
        f"[bold {render.STABLE}]Coherence timeline[/]  "
        f"[{render.MUTED}]({days}d, {sum(len(ss) for ss in by_day.values())} audits "
        f"across {len(by_day)} days)[/]"
    )
    console.print()

    for day in sorted(by_day.keys(), reverse=True):
        ss = by_day[day]
        clean = sum(1 for s in ss if s["status"] == "clean")
        drift = sum(1 for s in ss if s["status"] == "drift")
        lock = sum(1 for s in ss if s["status"] == "lockdown")
        n = len(ss)

        # Glyph strip: one mark per snapshot, in chronological order
        # (oldest first within the day).
        strip = ""
        for s in reversed(ss):
            if s["status"] == "lockdown":
                strip += f"[{render.ALARM}]✗[/]"
            elif s["status"] == "drift":
                strip += f"[{render.DRIFT}]⚠[/]"
            else:
                strip += f"[{render.STABLE}]✓[/]"

        summary = []
        if clean:
            summary.append(f"[{render.STABLE}]{clean} ✓[/]")
        if drift:
            summary.append(f"[{render.DRIFT}]{drift} ⚠[/]")
        if lock:
            summary.append(f"[{render.ALARM}]{lock} ✗[/]")
        sumstr = "  ".join(summary)

        console.print(
            f"  [{render.MUTED}]{day}[/]  {strip:60s}  "
            f"[{render.MUTED}]{n} audit{'s' if n != 1 else ''}[/]  {sumstr}"
        )


# ---------------------------------------------------------------------------
# `sym log` (E4)
# ---------------------------------------------------------------------------


@main.command("log")
@click.option("--since", default=None, help="ISO-8601 UTC lower bound (inclusive).")
@click.option("--limit", default=50, type=int, show_default=True)
def log_cmd(since: str | None, limit: int) -> None:
    """Chronological journal of events + narratives."""
    console = Console()
    try:
        with db.connect() as conn:
            events = db.list_events(conn, since=since, limit=limit)
            nars = db.list_narratives(conn, since=since, limit=limit)
    except Exception as e:
        console.print(f"[{render.ALARM}]database read failed: {e}[/]")
        raise SystemExit(2)

    if not events and not nars:
        console.print(
            f"[{render.MUTED}]no journal entries"
            f"{' since ' + since if since else ''}[/]"
        )
        return

    # Merge by timestamp, newest first.
    items: list[tuple[str, str, str]] = []
    for e in events:
        items.append(
            (
                e["occurred_at"],
                "event",
                f"{e['kind']}  {e.get('repo') or '-'}  {e['detail']}",
            )
        )
    for n in nars:
        items.append(
            (n["generated_at"], "narrative", f"({n['trigger']}) {n['text']}")
        )
    items.sort(key=lambda r: r[0], reverse=True)

    for ts, kind, body in items[:limit]:
        kind_style = render.STABLE if kind == "narrative" else render.MUTED
        console.print(
            f"[{render.MUTED}]{ts}[/] [{kind_style}]{kind:9s}[/] {body}"
        )
