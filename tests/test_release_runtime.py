from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_metadata", ROOT / ".github/scripts/release_metadata.py")
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text(json.dumps({"version": "1.2.3"}))
    (tmp_path / "frontend/package-lock.json").write_text(json.dumps({
        "version": "1.2.3", "packages": {"": {"version": "1.2.3"}},
    }))
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "add", "frontend")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture")
    sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "tag", "v1.2.3")
    return tmp_path, sha


def test_release_tag_matches_checkout_and_manifest(checkout: tuple[Path, str]) -> None:
    root, sha = checkout
    assert release.release_metadata(root, "push", "refs/tags/v1.2.3", sha) == {
        "image_tag": "v1.2.3", "latest": "true",
    }


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v1.2.3"])
def test_manual_build_cannot_replace_release_or_latest(checkout: tuple[Path, str], ref: str) -> None:
    root, sha = checkout
    assert release.release_metadata(root, "workflow_dispatch", ref, sha) == {
        "image_tag": f"manual-{sha}", "latest": "false",
    }


def test_old_release_does_not_replace_latest(checkout: tuple[Path, str]) -> None:
    root, sha = checkout
    git(root, "tag", "v1.10.0")
    assert release.release_metadata(root, "push", "refs/tags/v1.2.3", sha)["latest"] == "false"


@pytest.mark.parametrize("event,ref,sha_override,error", [
    ("push", "refs/heads/main", None, "requires a release tag"),
    ("push", "refs/tags/v1.2.4", None, "does not match the application"),
    ("workflow_dispatch", "refs/tags/v1.2.4", None, "does not match the application"),
    ("push", "refs/tags/v1.2.3", "a" * 40, "workflow commit"),
    ("pull_request", "refs/tags/v1.2.3", None, "tag push or manual"),
])
def test_invalid_release_is_rejected(checkout: tuple[Path, str], event: str, ref: str, sha_override: str | None, error: str) -> None:
    root, sha = checkout
    with pytest.raises(ValueError, match=error):
        release.release_metadata(root, event, ref, sha_override or sha)


def test_moved_tag_is_rejected(checkout: tuple[Path, str]) -> None:
    root, sha = checkout
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "next")
    git(root, "tag", "--force", "v1.2.3")
    git(root, "checkout", "--detach", sha)
    with pytest.raises(ValueError, match="tag does not point"):
        release.release_metadata(root, "push", "refs/tags/v1.2.3", sha)


@pytest.mark.parametrize("lock", [
    {"version": "1.2.2", "packages": {"": {"version": "1.2.3"}}},
    {"version": "1.2.3", "packages": {"": {"version": "1.2.2"}}},
])
def test_manifest_lock_mismatch_blocks_publication(checkout: tuple[Path, str], lock: dict) -> None:
    root, sha = checkout
    (root / "frontend/package-lock.json").write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="versions differ"):
        release.release_metadata(root, "push", "refs/tags/v1.2.3", sha)


@pytest.mark.parametrize("version,expected", [((3, 10, 9), 1), ((3, 11, 9), 1), ((3, 12, 0), 0), ((3, 13, 0), 0)])
def test_direct_backend_import_enforces_minimum_python(version: tuple[int, ...], expected: int) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.version_info = {version!r}; import backend"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == expected
    if expected:
        assert "Python 3.12" in result.stderr
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize("script", ["install.sh", "update.sh", "start.sh"])
def test_shell_entrypoints_reject_old_existing_venv_before_changes(tmp_path: Path, script: str) -> None:
    shutil.copy(ROOT / script, tmp_path / script)
    (tmp_path / "backend").mkdir()
    shutil.copy(ROOT / "backend/runtime.py", tmp_path / "backend/runtime.py")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    code = "import runpy, sys; sys.version_info = (3, 11, 9); runpy.run_path(sys.argv[1], run_name='__main__')"
    shim = tmp_path / ".venv/bin/python"
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} \"$@\"\n")
    shim.chmod(0o755)
    (tmp_path / ".env").write_text("KEEP=original\n")
    result = subprocess.run(["bash", str(tmp_path / script)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Python 3.12" in result.stderr
    assert result.stdout == ""
    assert (tmp_path / ".env").read_text() == "KEEP=original\n"
