"""Load `~/.symmetism/config/*.toml` and expose default _command/ location.

Layout (per `_command/03_SOT_STACK.md` S8):
    %USERPROFILE%/.symmetism/
        config/
            repos.toml      ← which repos and their remotes
            servers.toml    ← deploy targets (Phase F)
        secrets/            ← never read here
        state/              ← SQLite, status.json (Phase E/J)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _default_root() -> Path:
    """Where ~/.symmetism/ lives (env-overridable for tests)."""
    if env := os.environ.get("SYMVERIFY_HOME"):
        return Path(env)
    return Path.home() / ".symmetism"


def config_dir() -> Path:
    return _default_root() / "config"


def state_dir() -> Path:
    return _default_root() / "state"


def secrets_dir() -> Path:
    return _default_root() / "secrets"


def command_dir() -> Path:
    """Location of `C:\\Symmetism\\_command\\` (the SoT folder).

    Env-overridable via SYMVERIFY_COMMAND_DIR for tests / non-default
    layouts. Defaults to `C:\\Symmetism\\_command` since that's the
    fixed-by-spec location.
    """
    if env := os.environ.get("SYMVERIFY_COMMAND_DIR"):
        return Path(env)
    return Path("C:/Symmetism/_command")


@dataclass(frozen=True, slots=True)
class RepoConfig:
    name: str
    path: Path
    remote: str
    owner_type: str = "user"


def load_repos() -> dict[str, RepoConfig]:
    """Parse repos.toml. Empty dict if the file is missing."""
    p = config_dir() / "repos.toml"
    if not p.is_file():
        return {}
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    repos = data.get("repos", {})
    return {
        name: RepoConfig(
            name=name,
            path=Path(spec["path"]),
            remote=spec["remote"],
            owner_type=spec.get("owner_type", "user"),
        )
        for name, spec in repos.items()
    }
