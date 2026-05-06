"""State collection — manifests, invariants, server polls.

Extracted from cli.py so the daemon (which doesn't render anything)
can share the same probe logic.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from symverify import __version__, config, fingerprint as fp, git_ops, manifest
from symverify.stabilizer import State


def collect_invariants(repo_path: Path, version: str) -> dict[str, str]:
    """Cross-repo invariants for Q_cross_repo: SymVerify version,
    canonical schema, license SHA-256 (when LICENSE present)."""
    inv: dict[str, str] = {"symverify_version": version}
    mc = repo_path / "MANIFEST_CANONICAL.json"
    if mc.is_file():
        try:
            data = json.loads(mc.read_text(encoding="utf-8"))
            schema = data.get("schema", "")
            if schema:
                inv["manifest_canonical_schema"] = schema
        except Exception:
            pass
    lic = repo_path / "LICENSE"
    if lic.is_file():
        inv["license_sha256"] = hashlib.sha256(lic.read_bytes()).hexdigest()
    return inv


def build_state(
    repos: dict[str, config.RepoConfig],
    servers: dict[str, config.ServerConfig] | None = None,
) -> tuple[State, dict[str, dict]]:
    """Compute manifests + invariants + (optional) server manifests.

    Returns (state, per_repo_metadata) where metadata holds short SHAs,
    trinity fingerprints, and server commit_sha+manifest_root_hash for
    display. Wraps every probe in try/except so a transient network
    failure doesn't bring the daemon down.
    """
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
        invariants = collect_invariants(rc.path, __version__)
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

    # Poll deployed servers (best-effort).
    if servers:
        for sname, sc in servers.items():
            if not sc.token_file.is_file():
                continue
            try:
                token = sc.token_file.read_text(encoding="utf-8").strip()
                body = manifest.compute_server_manifest(
                    sc.url, token, manifest_path=sc.manifest_path
                )
            except Exception:
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


def system_fold(state: State, meta: dict[str, dict]) -> str | None:
    """Convenience: compute the 16-char SYM-... fold from the meta + state."""
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
        return None
    return fp.system_fold(trinities, invariants, __version__)
