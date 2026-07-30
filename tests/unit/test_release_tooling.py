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
from scripts.release_set import publish_release_set, validate_release_set
from scripts.release_support import (
    Package,
    ReleaseError,
    discover_packages,
    parse_release_tag,
    validate_release_artifacts,
)
from tests.support.ci_selectors import (
    PARSL_FAST_TEST_COMMAND,
    PARSL_SLOW_TEST_COMMAND,
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


def _create_release_set_repository(
    tmp_path: Path,
    *,
    application_dependency: str = "demo-core>=1.2.3,<2",
) -> Path:
    root = tmp_path / "release-set-repository"
    projects = {
        "demo-core": '[project]\nname = "demo-core"\nversion = "1.2.3"\n',
        "demo-app": (
            '[project]\nname = "demo-app"\nversion = "2.0.0"\n'
            f'dependencies = ["{application_dependency}"]\n'
        ),
    }
    for name, content in projects.items():
        package_dir = root / "packages" / name
        package_dir.mkdir(parents=True)
        (package_dir / "pyproject.toml").write_text(content)
    _run(root, "git", "init")
    _run(root, "git", "config", "user.email", "release@example.invalid")
    _run(root, "git", "config", "user.name", "Release Test")
    _run(root, "git", "add", "packages")
    _run(root, "git", "commit", "-m", "release package set")
    _run(root, "git", "tag", "-a", "demo-core-v1.2.3", "-m", "Release core")
    _run(root, "git", "tag", "-a", "demo-app-v2.0.0", "-m", "Release app")
    return root


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


def test_release_set_validates_one_commit_and_dependency_order(tmp_path: Path) -> None:
    root = _create_release_set_repository(tmp_path)

    plan = validate_release_set(
        ["demo-app-v2.0.0", "demo-core-v1.2.3"],
        root=root,
    )

    assert [item.package.name for item in plan.items] == ["demo-app", "demo-core"]
    assert plan.publish_order == ("demo-core", "demo-app")


def test_release_set_rejects_duplicate_package(tmp_path: Path) -> None:
    root = _create_release_set_repository(tmp_path)

    with pytest.raises(ReleaseError, match="selected more than once"):
        validate_release_set(
            ["demo-core-v1.2.3", "demo-core-v1.2.3"],
            root=root,
        )


def test_release_set_rejects_incompatible_selected_dependency(tmp_path: Path) -> None:
    root = _create_release_set_repository(
        tmp_path,
        application_dependency="demo-core>=1.3.0,<2",
    )

    with pytest.raises(ReleaseError, match="does not accept"):
        validate_release_set(
            ["demo-core-v1.2.3", "demo-app-v2.0.0"],
            root=root,
        )


def test_release_set_rejects_newer_remote_version(tmp_path: Path) -> None:
    root = _create_release_set_repository(tmp_path)

    with pytest.raises(ReleaseError, match="already has newer"):
        validate_release_set(
            ["demo-core-v1.2.3", "demo-app-v2.0.0"],
            root=root,
            remote_versions={"demo-core": "1.2.4", "demo-app": "1.9.0"},
        )


def test_release_set_publishes_validated_artifacts_in_dependency_order(
    tmp_path: Path,
) -> None:
    root = _create_release_set_repository(tmp_path)
    plan = validate_release_set(
        ["demo-core-v1.2.3", "demo-app-v2.0.0"],
        root=root,
    )
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    for item in plan.items:
        artifact_dir = artifact_root / f"release-{item.package.name}-{item.version}"
        _write_artifacts(artifact_dir, item.package)
    commands: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    published = publish_release_set(plan, artifact_root, runner=runner)

    assert published == ["demo-core", "demo-app"]
    assert "demo_core-1.2.3" in " ".join(commands[0])
    assert "demo_app-2.0.0" in " ".join(commands[1])
    assert all("--trusted-publishing" in command for command in commands)


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


def test_github_release_workflow_coordinates_validated_package_sets() -> None:
    root = Path(__file__).parents[2]
    workflow = _workflow(root, "release.yml")
    trigger = workflow["on"]
    jobs = workflow["jobs"]
    assert isinstance(trigger, dict)
    assert isinstance(jobs, dict)

    prepare = jobs["prepare"]
    build = jobs["build"]
    publish = jobs["publish"]
    assert isinstance(prepare, dict)
    assert isinstance(build, dict)
    assert isinstance(publish, dict)
    prepare_script = _job_script(prepare)
    build_script = _job_script(build)
    publish_script = _job_script(publish)

    assert set(trigger) == {"workflow_dispatch"}
    assert trigger["workflow_dispatch"]["inputs"]["mode"]["options"] == [
        "validate",
        "publish",
    ]
    assert 'git checkout --detach "$release_sha"' in prepare_script
    assert "scripts/release_set.py plan --check-pypi" in prepare_script
    assert "actions/workflows/ci.yml/runs" in prepare_script
    assert 'head_sha="$RELEASE_SHA"' in prepare_script
    assert "select(.conclusion == \"success\")" in prepare_script
    assert build["needs"] == "prepare"
    assert publish["needs"] == ["prepare", "build"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    assert "scripts/check_package_release.py" in build_script
    assert 'uv build --package "$RELEASE_PACKAGE" --no-sources' in build_script
    download_step = next(
        step
        for step in publish["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download_step["with"]["path"] == "${{ runner.temp }}/release"
    assert "scripts/release_set.py publish" in publish_script
    assert '--artifacts-dir "$RUNNER_TEMP/release"' in publish_script
    assert "scripts/release_set.py verify" in publish_script
    assert "UV_PUBLISH_TOKEN" not in prepare_script + build_script + publish_script
    release_set_source = (root / "scripts" / "release_set.py").read_text()
    assert '"--trusted-publishing"' in release_set_source
    assert '"always"' in release_set_source
    assert not (root / ".gitlab-ci.yml").exists()


def test_github_workflows_cover_normal_and_complete_validation() -> None:
    root = Path(__file__).parents[2]
    ci = _workflow(root, "ci.yml")
    complete = _workflow(root, "complete.yml")

    assert set(ci["jobs"]) == {
        "quality",
        "unit-tests",
        "integration-tests",
        "compatibility-tests",
        "parsl-fast-tests",
        "parsl-process-tests",
        "deterministic-tests",
        "packages",
        "docs",
    }
    assert set(complete["jobs"]) == {
        "wetlands",
        "public-data",
        "external-binaries",
        "model-runtimes",
    }

    parsl_fast = ci["jobs"]["parsl-fast-tests"]
    parsl_process = ci["jobs"]["parsl-process-tests"]
    assert isinstance(parsl_fast, dict)
    assert isinstance(parsl_process, dict)
    assert PARSL_FAST_TEST_COMMAND in _job_script(parsl_fast)
    assert PARSL_SLOW_TEST_COMMAND in _job_script(parsl_process)


def test_complete_workflow_schedules_only_resource_dependent_suites() -> None:
    root = Path(__file__).parents[2]
    complete = _workflow(root, "complete.yml")
    trigger = complete["on"]
    jobs = complete["jobs"]
    assert isinstance(trigger, dict)
    assert isinstance(jobs, dict)

    assert trigger["schedule"] == [{"cron": "0 3 * * 1"}]

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
