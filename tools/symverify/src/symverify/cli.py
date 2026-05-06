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

from symverify import db, narrative


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


def _collect_invariants(repo_path: Path, version: str) -> dict[str, str]:
    """Pick a few cross-repo invariants for Q_cross_repo.

    Both repos should have identical SymVerify version (we run one
    binary) and identical MANIFEST_CANONICAL schema string. License
    SHA-256 is included only if the repo has a LICENSE file.
    """
    import hashlib
    import json as _json

    inv: dict[str, str] = {"symverify_version": version}
    mc = repo_path / "MANIFEST_CANONICAL.json"
    if mc.is_file():
        try:
            data = _json.loads(mc.read_text(encoding="utf-8"))
            schema = data.get("schema", "")
            if schema:
                inv["manifest_canonical_schema"] = schema
        except Exception:
            pass
    lic = repo_path / "LICENSE"
    if lic.is_file():
        inv["license_sha256"] = hashlib.sha256(lic.read_bytes()).hexdigest()
    return inv


def _build_state(
    repos: dict[str, config.RepoConfig],
    servers: dict[str, config.ServerConfig] | None = None,
) -> tuple[State, dict[str, dict]]:
    """Compute manifests + invariants for both repos, then poll any
    configured servers' /__manifest endpoints (F12). Returns
    (state, per_repo_metadata) where metadata holds short SHAs, trinity
    fingerprints, and server commit_sha+manifest_root_hash for display.
    """
    from symverify import git_ops

    state = State(symverify_version=__version__)
    meta: dict[str, dict] = {}
    for name, rc in repos.items():
        if not rc.path.is_dir():
            meta[name] = {"error": f"path missing: {rc.path}"}
            continue
        try:
            local_m = manifest.compute_local_manifest(rc.path)
            git_m = manifest.compute_git_manifest(rc.path)
            head_sha = git_ops.git_head_sha(rc.path)
        except Exception as e:
            meta[name] = {"error": str(e)}
            continue
        local_h = local_m.root_hash()
        git_h = git_m.root_hash()
        meta[name] = {
            "local": local_h,
            "git": git_h,
            "server": None,
            "server_commit_sha": None,
            "short_sha": head_sha[:8],
            "trinity": fp.trinity_fingerprint(local_h, git_h, None),
        }
        invariants = _collect_invariants(rc.path, __version__)
        if name == "reflexivity":
            state.reflexivity_path = rc.path
            state.reflexivity_local_hash = local_h
            state.reflexivity_git_hash = git_h
            state.reflexivity_head_sha = head_sha
            state.invariants_R = invariants
        elif name == "platform":
            state.platform_path = rc.path
            state.platform_local_hash = local_h
            state.platform_git_hash = git_h
            state.platform_head_sha = head_sha
            state.invariants_P = invariants

    # --- F12: poll deployed apps' /__manifest -----------------------------
    if servers:
        for sname, sc in servers.items():
            if not sc.token_file.is_file():
                continue
            token = sc.token_file.read_text(encoding="utf-8").strip()
            try:
                body = manifest.compute_server_manifest(
                    sc.url, token, manifest_path=sc.manifest_path
                )
            except manifest.ServerManifestError:
                continue
            commit_sha = body.get("commit_sha")
            mh = body.get("manifest_root_hash")
            if sc.repo == "reflexivity":
                state.reflexivity_server_hash = mh
                state.reflexivity_server_commit_sha = commit_sha
                meta.setdefault("reflexivity", {})
                meta["reflexivity"]["server"] = mh
                meta["reflexivity"]["server_commit_sha"] = commit_sha
                meta["reflexivity"]["server_url"] = sc.url
                # Recompute trinity now that we have all three legs.
                if mh and meta["reflexivity"].get("local") and meta["reflexivity"].get("git"):
                    meta["reflexivity"]["trinity"] = fp.trinity_fingerprint(
                        meta["reflexivity"]["local"],
                        meta["reflexivity"]["git"],
                        mh,
                    )
            elif sc.repo == "platform":
                state.platform_server_hash = mh
                state.platform_server_commit_sha = commit_sha
                meta.setdefault("platform", {})
                meta["platform"]["server"] = mh
                meta["platform"]["server_commit_sha"] = commit_sha
                meta["platform"]["server_url"] = sc.url
                if mh and meta["platform"].get("local") and meta["platform"].get("git"):
                    meta["platform"]["trinity"] = fp.trinity_fingerprint(
                        meta["platform"]["local"],
                        meta["platform"]["git"],
                        mh,
                    )

    return state, meta


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

    # System fingerprint = first repo's trinity for now (sym fold lands at G1)
    first_meta = next(iter(meta.values()), {})
    fingerprint = first_meta.get("trinity", "<no-trinity>")

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
