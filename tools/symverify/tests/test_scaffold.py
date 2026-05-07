"""Tests for `sym scaffold` (F11.2)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from symverify import scaffold


@pytest.fixture
def fake_repo(tmp_path: Path):
    """Stub config.load_repos so scaffold writes into tmp_path."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "apps").mkdir()

    class _RC:
        def __init__(self, p): self.path = p
    repos = {"platform": _RC(repo_path)}

    with patch.object(scaffold.config, "load_repos", return_value=repos):
        yield repo_path


def test_scaffold_writes_full_skeleton(fake_repo: Path):
    result = scaffold.scaffold_app("platform/coherence-dashboard")

    assert result.app_name == "coherence-dashboard"
    assert result.repo_name == "platform"
    assert result.app_dir == fake_repo / "apps" / "coherence-dashboard"

    # Required files exist.
    expected = {
        "pyproject.toml",
        "Dockerfile",
        "README.md",
        "caddy-snippet.txt",
        "src/coherence_dashboard/__init__.py",
        "src/coherence_dashboard/main.py",
        "src/coherence_dashboard/manifest_endpoint.py",
    }
    for rel in expected:
        assert (result.app_dir / rel).is_file(), f"missing {rel}"

    # Pyproject contains the app name.
    pyproject = (result.app_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "coherence-dashboard"' in pyproject
    assert 'packages = ["src/coherence_dashboard"]' in pyproject

    # main.py imports manifest_endpoint and has /healthz.
    main_py = (result.app_dir / "src/coherence_dashboard/main.py").read_text(encoding="utf-8")
    assert "from coherence_dashboard.manifest_endpoint import attach_manifest_endpoint" in main_py
    assert "@app.get(\"/healthz\")" in main_py

    # Dockerfile parameterized correctly.
    dockerfile = (result.app_dir / "Dockerfile").read_text(encoding="utf-8")
    assert "apps/coherence-dashboard/" in dockerfile
    assert "coherence_dashboard.main:app" in dockerfile
    assert "Symmetism/Platform" in dockerfile

    # Caddy snippet has the host block.
    caddy = result.caddy_snippet_path.read_text(encoding="utf-8")
    assert "coherence-dashboard.symmetism.com" in caddy


def test_scaffold_refuses_clobber(fake_repo: Path):
    target_dir = fake_repo / "apps" / "existing-app"
    target_dir.mkdir(parents=True)

    with pytest.raises(scaffold.ScaffoldError, match="refusing to clobber"):
        scaffold.scaffold_app("platform/existing-app")


def test_scaffold_force_overwrites(fake_repo: Path):
    target_dir = fake_repo / "apps" / "myapp"
    target_dir.mkdir(parents=True)
    (target_dir / "old.txt").write_text("stale", encoding="utf-8")

    result = scaffold.scaffold_app("platform/myapp", force=True)
    assert (result.app_dir / "Dockerfile").is_file()
    # We don't delete unrelated files — old.txt survives unless overwritten.
    assert (target_dir / "old.txt").is_file()


@pytest.mark.parametrize(
    "name,ok",
    [
        ("foo", True),
        ("foo-bar", True),
        ("foo-bar-baz", True),
        ("a", False),               # too short
        ("Foo", False),             # uppercase
        ("foo_bar", False),         # underscore
        ("1foo", False),            # leading digit
        ("-foo", False),            # leading dash
        ("foo bar", False),         # space
    ],
)
def test_app_name_validation(fake_repo: Path, name: str, ok: bool):
    target = f"platform/{name}"
    if ok:
        scaffold.scaffold_app(target)
    else:
        with pytest.raises(scaffold.ScaffoldError, match="must match"):
            scaffold.scaffold_app(target)


def test_scaffold_unknown_repo(fake_repo: Path):
    with pytest.raises(scaffold.ScaffoldError, match="not configured"):
        scaffold.scaffold_app("nonexistent/foo")


def test_scaffold_missing_slash(fake_repo: Path):
    with pytest.raises(scaffold.ScaffoldError, match="expected"):
        scaffold.scaffold_app("just-an-app-name")


def test_scaffold_writes_lf_line_endings(fake_repo: Path):
    """Regression: on Windows, Path.write_text translates \\n → \\r\\n in
    text mode, which conflicts with the repo's `eol=lf` .gitattributes
    rule and forces a re-checkout after every scaffold. Files MUST be
    written with LF only (\\n) so local==git is preserved byte-for-byte
    on every platform."""
    result = scaffold.scaffold_app("platform/lf-test")
    for f in result.files_written:
        data = f.read_bytes()
        assert b"\r\n" not in data, (
            f"{f.relative_to(result.app_dir)} contains CRLF — "
            f"would force `git reset --hard` after every scaffold"
        )
