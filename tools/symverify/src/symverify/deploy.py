"""`sym deploy` — apply local Platform/server/ changes to the VPS.

Codifies the SSH + SFTP + sudo + docker-compose flow that the rename
test required us to write inline as a one-off Python script. After
this exists, every infra change is `sym deploy` instead of "open
Python, write a script, debug SSH, hope sudo works."

What it does, in order:
  1. Connect to the VPS as `<user>` using the ed25519 deploy key.
  2. SFTP-upload local `Platform/server/` files to /tmp/symverify-deploy/.
  3. sudo (with password from ~/.symmetism/secrets/vps.sudo.pass) to:
       - rsync the staged tree into <remote_root>
       - drop any orphaned per-app compose files on the VPS that
         aren't in the new include list
       - migrate <remote_envs>/<old>.env → <new>.env when an app
         was renamed (best-effort, based on compose include diff)
       - create <remote_envs>/<new>.env from the local
         secrets/symverify.<new>.token when a new app appears
       - `docker compose pull && docker compose up -d --remove-orphans`
       - `docker restart <caddy_container>` so the new Caddyfile is
         picked up (Caddy admin API is off in our setup)
  4. Print final `docker ps` for confirmation.

Idempotent: re-running with no local changes is a no-op (rsync sees
no diffs, compose up does nothing if services are unchanged).

Doesn't touch GHCR, GHA, or DNS — those are git-driven and Cloudflare-
side respectively.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from symverify import config


class DeployError(RuntimeError):
    """Surfaces any unrecoverable deploy step failure."""


# ---------------------------------------------------------------------------
# SSH primitives
# ---------------------------------------------------------------------------


class _Conn:
    """Wraps a paramiko SSH client + lazily-opened SFTP, with a sudo helper."""

    REMOTE_STAGE = "/tmp/symverify-deploy"

    def __init__(self, dc: config.DeployConfig, on_event: Callable | None = None):
        # Defer paramiko import so the rest of the CLI works without it.
        try:
            import paramiko
        except ImportError as e:
            raise DeployError(
                f"paramiko not installed ({e}); add it to symverify's deps."
            ) from e
        self._paramiko = paramiko
        self._dc = dc
        self._on_event = on_event or (lambda *_a, **_k: None)
        self._client: Any = None
        self._sftp: Any = None
        self._password: str = ""

    def __enter__(self) -> "_Conn":
        # Read sudo password (raw bytes → strip → decode).
        if not self._dc.sudo_pass_file.is_file():
            raise DeployError(f"sudo_pass_file missing: {self._dc.sudo_pass_file}")
        self._password = self._dc.sudo_pass_file.read_bytes().rstrip().decode()

        # Copy the SSH key to a temp location with 600 perms — Windows
        # checkouts don't preserve mode 600 and OpenSSH (and paramiko
        # in some configs) refuses overly-permissive keys.
        if not self._dc.ssh_key.is_file():
            raise DeployError(f"ssh_key missing: {self._dc.ssh_key}")
        import os
        import tempfile
        fd, tmp_key = tempfile.mkstemp(prefix="symverify-key-", suffix="")
        os.close(fd)
        self._tmp_key = Path(tmp_key)
        shutil.copy(self._dc.ssh_key, self._tmp_key)
        os.chmod(self._tmp_key, 0o600)

        self._client = self._paramiko.SSHClient()
        self._client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        self._client.connect(
            self._dc.host,
            username=self._dc.user,
            key_filename=str(self._tmp_key),
            timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
        return self

    def __exit__(self, *_) -> None:
        try:
            if self._sftp:
                self._sftp.close()
        finally:
            try:
                self._client.close()
            finally:
                try:
                    self._tmp_key.unlink()
                except Exception:
                    pass

    # --- sftp + sudo ----------------------------------------------------

    def sftp(self):
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def sudo(self, cmd: str, *, label: str = "", timeout: int = 240) -> tuple[int, str]:
        """Run `sudo -S sh -c '<cmd>'` piping the password.

        Returns (rc, cleaned_output). 'Cleaned' = sudo prompt + lecture
        + the password echo lines stripped.
        """
        if label:
            self._on_event("step", label)
        chan = self._client.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(f"sudo -S sh -c {cmd!r}")
        chan.send(self._password + "\n")
        out = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if chan.recv_ready():
                out += chan.recv(8192)
            if chan.exit_status_ready():
                # Drain any remaining output.
                time.sleep(0.3)
                while chan.recv_ready():
                    out += chan.recv(8192)
                break
            time.sleep(0.2)
        rc = chan.recv_exit_status()
        text = out.decode("utf-8", errors="replace")
        cleaned = _strip_sudo_noise(text, self._password)
        chan.close()
        if cleaned and self._on_event:
            self._on_event("output", cleaned[:4000])
        return rc, cleaned


def _strip_sudo_noise(text: str, password: str) -> str:
    skip = (
        "[sudo] password",
        password,
        "We trust you have received the usual lecture",
        "#1) Respect the privacy of others.",
        "#2) Think before you type.",
        "#3) With great power comes great responsibility.",
        "#4) When in doubt, ask.",
    )
    return "\n".join(
        line for line in text.splitlines()
        if not any(s in line for s in skip if s)
    ).strip()


# ---------------------------------------------------------------------------
# Local file staging
# ---------------------------------------------------------------------------


def _walk_stack(local_root: Path) -> list[tuple[Path, str]]:
    """Return [(local_path, remote_relpath), ...] for every file under
    Platform/server/ that we want to mirror to /srv/symmetism/.

    Skips Compose include files for apps that no longer exist (we do
    that diff at apply time, not staging) and editor noise.
    """
    out: list[tuple[Path, str]] = []
    for p in sorted(local_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(local_root)
        rel_str = str(rel).replace("\\", "/")
        # Skip noise.
        if any(part.startswith(".") for part in rel.parts):
            continue
        if any(part in ("__pycache__", "node_modules") for part in rel.parts):
            continue
        out.append((p, rel_str))
    return out


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Apps detection (compose include diff)
# ---------------------------------------------------------------------------


def _included_apps(compose_text: str) -> set[str]:
    """Parse the `include:` block of a top-level compose.yaml and return
    {app_name, ...} where each include is `apps/<name>.compose.yaml`."""
    apps: set[str] = set()
    in_include = False
    for line in compose_text.splitlines():
        stripped = line.strip()
        if stripped == "include:":
            in_include = True
            continue
        if in_include:
            if stripped.startswith("- "):
                # Form: - apps/<name>.compose.yaml
                rel = stripped[2:].strip()
                if rel.startswith("apps/") and rel.endswith(".compose.yaml"):
                    name = rel[len("apps/") : -len(".compose.yaml")]
                    apps.add(name)
            elif stripped and not stripped.startswith("-"):
                # End of include block.
                break
    return apps


# ---------------------------------------------------------------------------
# High-level: deploy()
# ---------------------------------------------------------------------------


def deploy(
    *,
    dry_run: bool = False,
    on_event: Callable | None = None,
) -> dict[str, Any]:
    """Apply local Platform/server/ changes to the VPS. Returns a summary."""

    dc = config.load_deploy()
    if dc is None:
        raise DeployError(
            f"no deploy.toml at {config.config_dir() / 'deploy.toml'}.\n"
            "Create one — see symverify/deploy.py docstring for the schema."
        )
    if not dc.local_stack.is_dir():
        raise DeployError(f"local_stack missing: {dc.local_stack}")

    on_event = on_event or (lambda *_a, **_k: None)
    summary: dict[str, Any] = {
        "host": dc.host,
        "user": dc.user,
        "uploaded": 0,
        "skipped": 0,
        "renamed_envs": [],
        "new_envs": [],
        "removed_orphans": [],
        "dry_run": dry_run,
    }

    # 1. Stage local files.
    on_event("step", "scan local stack")
    files = _walk_stack(dc.local_stack)
    on_event("output", f"{len(files)} files under {dc.local_stack}")

    if dry_run:
        summary["staged_files"] = [r for _, r in files]
        on_event("step", "dry-run: not connecting")
        return summary

    # 2. Connect.
    on_event("step", f"ssh {dc.user}@{dc.host}")
    with _Conn(dc, on_event=on_event) as conn:
        # 2a. Detect orphan-able apps by reading the remote compose.yaml,
        # comparing its include list to the local one. Any apps in remote
        # not in local are orphans (need their compose file removed +
        # potentially their env file migrated).
        local_compose = (dc.local_stack / "compose.yaml").read_text(encoding="utf-8")
        local_apps = _included_apps(local_compose)
        on_event("output", f"local apps: {sorted(local_apps)}")

        try:
            with conn.sftp().open(f"{dc.remote_root}/compose.yaml", "r") as fh:
                remote_compose = fh.read().decode("utf-8")
            remote_apps = _included_apps(remote_compose)
        except (IOError, FileNotFoundError):
            remote_apps = set()
        on_event("output", f"remote apps: {sorted(remote_apps)}")

        new_apps = local_apps - remote_apps      # need new env files
        removed_apps = remote_apps - local_apps  # need orphan cleanup

        # 3. Stage uploads to /tmp/symverify-deploy/ via SFTP.
        on_event("step", "stage uploads to /tmp/symverify-deploy/")
        # Ensure the staging dir exists, fresh.
        conn.sudo(f"rm -rf {conn.REMOTE_STAGE} && mkdir -p {conn.REMOTE_STAGE}")
        # The staging dir is owned by root after sudo mkdir; chown so
        # symmetism can SFTP-write into it.
        conn.sudo(
            f"chown -R {dc.user}:{dc.user} {conn.REMOTE_STAGE}",
        )

        for local_path, rel in files:
            local_hash = _file_digest(local_path)
            # Skip if remote already has this exact byte content.
            remote_path = f"{dc.remote_root}/{rel}"
            try:
                with conn.sftp().open(remote_path, "rb") as fh:
                    remote_hash = hashlib.sha256(fh.read()).hexdigest()
                if remote_hash == local_hash:
                    summary["skipped"] += 1
                    continue
            except (IOError, FileNotFoundError):
                pass  # file is new; upload it
            # Upload to staging.
            stage_path = f"{conn.REMOTE_STAGE}/{rel}"
            stage_dir = "/".join(stage_path.split("/")[:-1])
            conn.sudo(f"mkdir -p {stage_dir} && chown {dc.user}:{dc.user} {stage_dir}")
            conn.sftp().put(str(local_path), stage_path)
            summary["uploaded"] += 1
        on_event("output", f"uploaded {summary['uploaded']} / skipped {summary['skipped']}")

        # 4. Apply staged files via sudo rsync (preserves permissions,
        # only changes what differs).
        on_event("step", "apply staged files to remote_root")
        rc, _ = conn.sudo(
            f"set -e; "
            f"rsync -a --no-owner --no-group {conn.REMOTE_STAGE}/ {dc.remote_root}/ && "
            f"chown -R root:root {dc.remote_root} && "
            f"rm -rf {conn.REMOTE_STAGE}",
            label="rsync staged → remote_root",
        )
        if rc != 0:
            raise DeployError(f"rsync failed: rc={rc}")

        # 5. Drop orphan compose files and migrate / create env files.
        for app in sorted(removed_apps):
            on_event("step", f"orphan: apps/{app}.compose.yaml")
            conn.sudo(f"rm -f {dc.remote_root}/apps/{app}.compose.yaml")
            summary["removed_orphans"].append(app)

        # If a single app was removed and a single app was added, treat
        # it as a rename and migrate the env file in place.
        if len(removed_apps) == 1 and len(new_apps) == 1:
            old = next(iter(removed_apps))
            new = next(iter(new_apps))
            on_event("step", f"migrate env: {old}.env → {new}.env")
            rc, _ = conn.sudo(
                f"if [ -f {dc.remote_envs}/{old}.env ]; then "
                f"  mv {dc.remote_envs}/{old}.env {dc.remote_envs}/{new}.env; "
                f"  echo 'migrated'; "
                f"else "
                f"  echo 'no source env file'; "
                f"fi"
            )
            summary["renamed_envs"].append({"old": old, "new": new})
            new_apps = set()  # already handled

        # Create env files for genuinely new apps from local secrets dir.
        for app in sorted(new_apps):
            tok_path = config.secrets_dir() / f"symverify.{app}.token"
            if not tok_path.is_file():
                on_event(
                    "warn",
                    f"new app '{app}' has no token at {tok_path}; skipping env",
                )
                continue
            tok = tok_path.read_text(encoding="utf-8").strip()
            on_event("step", f"create env: {app}.env")
            rc, _ = conn.sudo(
                f"echo 'SYMVERIFY_TOKEN={tok}' > {dc.remote_envs}/{app}.env && "
                f"chmod 600 {dc.remote_envs}/{app}.env && "
                f"chown root:root {dc.remote_envs}/{app}.env"
            )
            summary["new_envs"].append(app)

        # 6. Pull + up + restart Caddy.
        on_event("step", "docker compose pull")
        rc, _ = conn.sudo(
            f"cd {dc.remote_root} && docker compose pull 2>&1 | tail -20",
            timeout=300,
        )
        on_event("step", "docker compose up -d --remove-orphans")
        rc, _ = conn.sudo(
            f"cd {dc.remote_root} && docker compose up -d --remove-orphans 2>&1",
            timeout=300,
        )

        # Caddy admin is off in our setup, so reload-via-API doesn't work.
        # Restart the container to pick up the new Caddyfile bind-mount.
        on_event("step", f"restart {dc.caddy_container} (Caddy admin off)")
        conn.sudo(
            f"docker restart {dc.caddy_container} && sleep 2",
            timeout=60,
        )

        # 7. Final state.
        on_event("step", "docker ps")
        rc, ps = conn.sudo(
            "docker ps --format '{{.Names}}: {{.Image}} ({{.Status}})'",
        )
        summary["docker_ps"] = ps

    return summary
