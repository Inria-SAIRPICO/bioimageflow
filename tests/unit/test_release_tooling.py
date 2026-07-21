"""Release tooling contracts for independently versioned packages."""

from __future__ import annotations

import io
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

import pytest
import yaml

from scripts.check_package_release import validate_release
from scripts.package_status import package_status
from scripts.release_support import (
    Package,
    ReleaseError,
    discover_packages,
    parse_release_tag,
    validate_release_artifacts,
)


def _run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, capture_output=True, text=True)


def _workflow(root: Path, name: str) -> dict[str, object]:
    workflow = yaml.load(
        (root / ".github" / "workflows" / name).read_text(),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(workflow, dict)
    return workflow


def _job_script(job: dict[str, object]) -> str:
    steps = job["steps"]
    assert isinstance(steps, list)
    return "\n".join(
        str(step["run"])
        for step in steps
        if isinstance(step, dict) and "run" in step
    )


def _create_release_repository(tmp_path: Path) -> tuple[Path, Package]:
    root = tmp_path / "repository"
    package_dir = root / "packages" / "demo-tools"
    package_dir.mkdir(parents=True)
    (package_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-tools"\nversion = "1.2.3"\n'
    )
    _run(root, "git", "init")
    _run(root, "git", "config", "user.email", "release@example.invalid")
    _run(root, "git", "config", "user.name", "Release Test")
    _run(root, "git", "add", "packages/demo-tools/pyproject.toml")
    _run(root, "git", "commit", "-m", "release demo-tools")
    _run(root, "git", "tag", "-a", "demo-tools-v1.2.3", "-m", "Release 1.2.3")
    [package] = discover_packages(root)
    return root, package


def _write_artifacts(artifact_dir: Path, package: Package) -> None:
    artifact_dir.mkdir()
    metadata = (
        f"Metadata-Version: 2.1\nName: {package.name}\nVersion: {package.version}\n\n"
    ).encode()
    wheel = artifact_dir / f"{package.normalized_name}-{package.version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{package.normalized_name}-{package.version}.dist-info/METADATA",
            metadata,
        )
    sdist = artifact_dir / f"{package.normalized_name}-{package.version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"{package.name}-{package.version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_workspace_packages_are_discovered_independently() -> None:
    packages = discover_packages()

    assert len(packages) == 10
    assert {package.name for package in packages} == {
        "bioimageflow",
        "bioimageflow-common-tools",
        "bioimageflow-core",
        "bioimageflow-io-tools",
        "bioimageflow-measurement-tools",
        "bioimageflow-restoration-tools",
        "bioimageflow-sairpico-tools",
        "bioimageflow-segmentation-tools",
        "bioimageflow-spot-tools",
        "bioimageflow-tracking-tools",
    }


def test_release_tag_selects_exact_package_and_version() -> None:
    packages = discover_packages()

    package, version = parse_release_tag("bioimageflow-core-v0.1.6", packages)

    assert package.name == "bioimageflow-core"
    assert version == "0.1.6"


@pytest.mark.parametrize(
    "tag",
    [
        "v0.1.6",
        "bioimageflow-core-0.1.6",
        "bioimageflow-core-v0.1",
        "unknown-package-v0.1.6",
    ],
)
def test_invalid_release_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ReleaseError, match="Invalid release tag"):
        parse_release_tag(tag, discover_packages())


def test_release_validation_requires_matching_annotated_tag_at_head(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    selection = validate_release(package.release_tag, root=root)

    assert selection.package == package
    assert selection.version == "1.2.3"


def test_release_validation_rejects_changes_after_tag(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)
    (package.directory / "module.py").write_text("value = 1\n")

    with pytest.raises(ReleaseError, match="Working tree must be clean"):
        validate_release(package.release_tag, root=root)


def test_release_artifacts_must_be_exactly_one_matching_pair(tmp_path: Path) -> None:
    _, package = _create_release_repository(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    _write_artifacts(artifact_dir, package)

    artifacts = validate_release_artifacts(artifact_dir, package, package.version)

    assert len(artifacts) == 2


def test_release_artifacts_reject_another_distribution(tmp_path: Path) -> None:
    _, package = _create_release_repository(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    _write_artifacts(artifact_dir, package)
    (artifact_dir / "other-1.0.0-py3-none-any.whl").write_bytes(b"not a wheel")

    with pytest.raises(ReleaseError, match="exactly one wheel"):
        validate_release_artifacts(artifact_dir, package, package.version)


def test_status_uses_package_specific_tag_to_detect_changes(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    assert package_status(package, "1.2.3", root)[0] == "up-to-date"

    (package.directory / "module.py").write_text("value = 1\n")
    assert package_status(package, "1.2.3", root)[0] == "bump-required"


def test_status_distinguishes_unpublished_pending_and_behind(tmp_path: Path) -> None:
    root, package = _create_release_repository(tmp_path)

    assert package_status(package, None, root)[0] == "unpublished"
    assert package_status(package, "1.2.2", root)[0] == "pending"
    assert package_status(package, "1.2.4", root)[0] == "behind"


def test_github_release_workflow_is_tag_scoped_and_uses_trusted_publishing() -> None:
    root = Path(__file__).parents[2]
    workflow = _workflow(root, "release.yml")
    trigger = workflow["on"]
    jobs = workflow["jobs"]
    assert isinstance(trigger, dict)
    assert isinstance(jobs, dict)

    build = jobs["build"]
    publish = jobs["publish"]
    assert isinstance(build, dict)
    assert isinstance(publish, dict)
    build_script = _job_script(build)
    publish_script = _job_script(publish)

    assert trigger["push"]["tags"] == ["bioimageflow*-v*"]
    assert publish["needs"] == "build"
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    assert "scripts/check_package_release.py" in build_script
    assert 'uv build --package "$RELEASE_PACKAGE" --no-sources' in build_script
    assert "BIOIMAGEFLOW_PACKAGE_ARTIFACTS_PACKAGE" in str(build["steps"])
    assert "--trusted-publishing always" in publish_script
    assert "dist/release/*" in publish_script
    assert "UV_PUBLISH_TOKEN" not in build_script + publish_script
    assert not (root / ".gitlab-ci.yml").exists()


def test_github_workflows_cover_normal_and_complete_validation() -> None:
    root = Path(__file__).parents[2]
    ci = _workflow(root, "ci.yml")
    complete = _workflow(root, "complete.yml")

    assert set(ci["jobs"]) == {
        "quality",
        "fast-tests",
        "deterministic-tests",
        "packages",
        "docs",
    }
    assert set(complete["jobs"]) == {
        "release-validation",
        "wetlands",
        "public-data",
        "external-binaries",
        "model-runtimes",
    }


def test_complete_workflow_schedules_only_resource_dependent_suites() -> None:
    root = Path(__file__).parents[2]
    complete = _workflow(root, "complete.yml")
    trigger = complete["on"]
    jobs = complete["jobs"]
    assert isinstance(trigger, dict)
    assert isinstance(jobs, dict)

    assert trigger["schedule"] == [{"cron": "0 3 * * 1"}]

    release_validation = jobs["release-validation"]
    assert isinstance(release_validation, dict)
    assert "workflow_dispatch" in release_validation["if"]
    assert "schedule" not in release_validation["if"]

    for name in ("wetlands", "public-data", "external-binaries", "model-runtimes"):
        job = jobs[name]
        assert isinstance(job, dict)
        assert "github.event_name == 'schedule'" in job["if"]
        assert job["continue-on-error"] == "${{ github.event_name == 'schedule' }}"


def test_github_actions_are_pinned_to_commit_shas() -> None:
    root = Path(__file__).parents[2]

    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        workflow = _workflow(root, path.name)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        for job in jobs.values():
            assert isinstance(job, dict)
            steps = job["steps"]
            assert isinstance(steps, list)
            for step in steps:
                if isinstance(step, dict) and "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
