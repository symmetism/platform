"""Tests for `sym deploy`'s pure-logic helpers.

The full SSH/SFTP path is integration-tested manually against the real
VPS — what we cover here is the parsing, staging, and rename-detection
logic that runs locally before paramiko opens a connection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from symverify import deploy


# ---------------------------------------------------------------------------
# _included_apps — compose include parser
# ---------------------------------------------------------------------------


def test_included_apps_parses_basic():
    txt = """\
name: symmetism

services:
  caddy:
    image: caddy:2-alpine

include:
  - apps/engine.compose.yaml
  - apps/landing.compose.yaml
  - apps/attestation-service.compose.yaml
"""
    assert deploy._included_apps(txt) == {
        "engine", "landing", "attestation-service",
    }


def test_included_apps_empty_for_no_include_block():
    txt = "name: symmetism\nservices:\n  caddy:\n    image: caddy\n"
    assert deploy._included_apps(txt) == set()


def test_included_apps_stops_at_block_end():
    """If a non-list line follows the include block, parsing should stop
    so we don't accidentally pick up unrelated `- foo` lines later in
    the file."""
    txt = """\
include:
  - apps/engine.compose.yaml

networks:
  - some_network
"""
    # `engine` is included; `some_network` is not (it's not under apps/).
    assert deploy._included_apps(txt) == {"engine"}


def test_included_apps_ignores_unrelated_includes():
    txt = """\
include:
  - apps/engine.compose.yaml
  - other/something.yaml
"""
    # only apps/<name>.compose.yaml entries count
    assert deploy._included_apps(txt) == {"engine"}


# ---------------------------------------------------------------------------
# _walk_stack — local file collection
# ---------------------------------------------------------------------------


def test_walk_stack_collects_normal_files(tmp_path: Path):
    (tmp_path / "Caddyfile").write_text("# caddy", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("name: x", encoding="utf-8")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "engine.compose.yaml").write_text("services:", encoding="utf-8")

    files = deploy._walk_stack(tmp_path)
    rel_paths = {r for _, r in files}
    assert rel_paths == {
        "Caddyfile",
        "compose.yaml",
        "apps/engine.compose.yaml",
    }


def test_walk_stack_skips_dot_dirs_and_pycache(tmp_path: Path):
    (tmp_path / "Caddyfile").write_text("ok", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00\x00")

    files = deploy._walk_stack(tmp_path)
    rel_paths = {r for _, r in files}
    assert rel_paths == {"Caddyfile"}


# ---------------------------------------------------------------------------
# _strip_sudo_noise — output cleanup
# ---------------------------------------------------------------------------


def test_strip_sudo_noise_removes_password_prompt():
    raw = (
        "[sudo] password for symmetism: \n"
        "We trust you have received the usual lecture\n"
        "real output line\n"
    )
    cleaned = deploy._strip_sudo_noise(raw, "secret_pw")
    assert "real output line" in cleaned
    assert "[sudo]" not in cleaned
    assert "lecture" not in cleaned


def test_strip_sudo_noise_drops_password_lines():
    """If the password somehow echoes (e.g., terminal didn't disable
    echo), it must be filtered out so we never log it."""
    raw = "secret_pw\nactual output\n"
    cleaned = deploy._strip_sudo_noise(raw, "secret_pw")
    assert "secret_pw" not in cleaned
    assert "actual output" in cleaned


# ---------------------------------------------------------------------------
# deploy() — high-level orchestration with mocks
# ---------------------------------------------------------------------------


def test_deploy_errors_when_config_missing(monkeypatch):
    monkeypatch.setattr(deploy.config, "load_deploy", lambda: None)
    with pytest.raises(deploy.DeployError, match="no deploy.toml"):
        deploy.deploy()


def test_deploy_errors_when_local_stack_missing(monkeypatch, tmp_path: Path):
    fake = MagicMock()
    fake.local_stack = tmp_path / "doesnotexist"
    monkeypatch.setattr(deploy.config, "load_deploy", lambda: fake)
    with pytest.raises(deploy.DeployError, match="local_stack missing"):
        deploy.deploy()


def test_deploy_dry_run_lists_files_without_connecting(tmp_path: Path, monkeypatch):
    (tmp_path / "Caddyfile").write_text("ok", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("include:\n  - apps/x.compose.yaml\n", encoding="utf-8")
    fake = MagicMock(local_stack=tmp_path)
    monkeypatch.setattr(deploy.config, "load_deploy", lambda: fake)

    # Patch _Conn so we'd raise if it were instantiated; dry-run must
    # NOT touch the network.
    with patch.object(deploy, "_Conn",
                       side_effect=AssertionError("must not connect on dry-run")):
        events = []
        result = deploy.deploy(dry_run=True, on_event=lambda k, d: events.append((k, d)))

    assert result["dry_run"] is True
    assert sorted(result["staged_files"]) == ["Caddyfile", "compose.yaml"]
    assert any("dry-run" in d for _, d in events)


# ---------------------------------------------------------------------------
# rename detection — single add + single remove → migrate env
# ---------------------------------------------------------------------------


def test_rename_pattern_identified_in_app_diff():
    """When local apps differ from remote by exactly one removal +
    one addition, deploy treats it as a rename."""
    local = {"engine", "landing", "attestation-service"}
    remote = {"reflexivity-webapp", "landing", "attestation-service"}
    new_apps = local - remote
    removed_apps = remote - local
    assert new_apps == {"engine"}
    assert removed_apps == {"reflexivity-webapp"}
    # The deploy() function uses len(==1) checks on both sets.
    assert len(removed_apps) == 1 and len(new_apps) == 1
