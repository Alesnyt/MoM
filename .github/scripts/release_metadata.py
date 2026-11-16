"""Validate the exact checkout before any release tests or registry writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def release_metadata(root: Path, event: str, ref: str, sha: str) -> dict[str, str]:
    if event not in {"push", "workflow_dispatch"}:
        raise ValueError("Image publication requires a tag push or manual run")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or _git(root, "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout does not match the workflow commit")

    package = json.loads((root / "frontend/package.json").read_text())
    lock = json.loads((root / "frontend/package-lock.json").read_text())
    version = package["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Application version must be a stable major.minor.patch version")
    if lock.get("version") != version or lock.get("packages", {}).get("", {}).get("version") != version:
        raise ValueError("package.json and package-lock.json versions differ")

    is_tag = ref.startswith("refs/tags/")
    if event == "push" and not is_tag:
        raise ValueError("Automatic image publication requires a release tag")
    if is_tag:
        if ref != f"refs/tags/v{version}":
            raise ValueError("Release tag does not match the application version")
        if _git(root, "rev-parse", f"{ref}^{{commit}}") != sha:
            raise ValueError("Release tag does not point to the workflow commit")

    # Manual runs always have an isolated tag, even when dispatched on a release
    # tag. Re-testing an old checkout must not overwrite a published release.
    if event == "workflow_dispatch":
        return {"image_tag": f"manual-{sha}", "latest": "false"}

    versions = []
    for tag in _git(root, "tag", "--list", "v*").splitlines():
        match = _VERSION.fullmatch(tag[1:])
        if match:
            versions.append(tuple(map(int, match.groups())))
    current = tuple(map(int, version.split(".")))
    return {"image_tag": f"v{version}", "latest": str(current == max(versions)).lower()}


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    result = release_metadata(root, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
