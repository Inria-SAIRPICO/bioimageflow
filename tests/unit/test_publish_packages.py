"""Contracts for the one-time local batch publisher."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import scripts.publish_packages as batch_publish
from scripts.publish_packages import (
    PublishDecision,
    build_publish_plan,
    publish_batch,
    select_packages,
    validate_target_version,
)
from scripts.release_support import Package, ReleaseError


def _package(tmp_path: Path, name: str, version: str) -> Package:
    directory = tmp_path / "packages" / name
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    return Package(name=name, version=version, directory=directory)


@pytest.mark.parametrize("version", ["1.2", "1.2.3rc1", "v1.2.3", "latest"])
def test_batch_target_requires_stable_three_part_version(version: str) -> None:
    with pytest.raises(ReleaseError, match="Invalid target version"):
        validate_target_version(version)


def test_batch_plan_skips_existing_versions_and_selects_missing_projects(
    tmp_path: Path,
) -> None:
    current = _package(tmp_path, "current", "1.2.3")
    newer = _package(tmp_path, "newer", "1.2.3")
    missing = _package(tmp_path, "missing", "1.2.3")

    decisions = build_publish_plan(
        "1.2.3",
        [current, newer, missing],
        {"current": "1.2.3", "newer": "1.3.0", "missing": None},
    )

    assert [decision.action for decision in decisions] == ["skip", "skip", "publish"]


def test_batch_plan_rejects_a_local_version_mismatch(tmp_path: Path) -> None:
    package = _package(tmp_path, "demo", "1.2.2")

    [decision] = build_publish_plan("1.2.3", [package], {"demo": None})

    assert decision.action == "error"
    assert "local version is 1.2.2" in decision.detail


def test_package_filters_reject_unknown_or_mixed_selection(tmp_path: Path) -> None:
    packages = [_package(tmp_path, "one", "1.2.3")]

    with pytest.raises(ReleaseError, match="Unknown package"):
        select_packages(packages, ["missing"], [])
    with pytest.raises(ReleaseError, match="either --package or --exclude-package"):
        select_packages(packages, ["one"], ["one"])


def test_local_publish_uses_token_environment_and_disables_oidc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(tmp_path, "demo", "1.2.3")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "release@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)
    decision = PublishDecision(package, None, "publish", "project is unpublished")
    commands: list[list[str]] = []
    command_environments: list[dict[str, str]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        command_environments.append(kwargs["env"])
        if command[:2] == ["uv", "build"]:
            artifact_dir = Path(command[command.index("--out-dir") + 1])
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "demo-1.2.3-py3-none-any.whl").touch()
            (artifact_dir / "demo-1.2.3.tar.gz").touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        batch_publish,
        "validate_release_artifacts",
        lambda artifact_dir, _package, _version: sorted(artifact_dir.iterdir()),
    )

    published = publish_batch(
        [decision],
        root=tmp_path,
        environ={"UV_PUBLISH_TOKEN": "secret"},
        runner=fake_run,
    )

    assert published == ["demo"]
    publish_command = commands[1]
    assert publish_command[:2] == ["uv", "publish"]
    assert "--trusted-publishing" in publish_command
    assert "never" in publish_command
    assert "--check-url" in publish_command
    assert "secret" not in publish_command
    assert "UV_PUBLISH_TOKEN" not in command_environments[0]
    assert command_environments[1]["UV_PUBLISH_TOKEN"] == "secret"


def test_local_publish_requires_token_and_clean_tree(tmp_path: Path) -> None:
    package = _package(tmp_path, "demo", "1.2.3")
    decision = PublishDecision(package, None, "publish", "project is unpublished")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    with pytest.raises(ReleaseError, match="UV_PUBLISH_TOKEN"):
        publish_batch([decision], root=tmp_path, environ={})
    with pytest.raises(ReleaseError, match="Working tree must be clean"):
        publish_batch([decision], root=tmp_path, environ={"UV_PUBLISH_TOKEN": "secret"})
