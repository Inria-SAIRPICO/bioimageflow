"""Safe release-set discovery and annotated-tag creation."""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import subprocess

import pytest
from packaging.requirements import Requirement

import scripts.release_set as release_set_module
from scripts.release_set import select_release_set, tag_release_set
from scripts.release_support import (
    ReleaseError,
    discover_packages,
    git_output,
    load_pyproject,
    tag_exists,
)


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(
    tmp_path: Path,
    projects: dict[str, tuple[str, Sequence[str]]],
) -> Path:
    root = tmp_path / "repository"
    for name, (version, dependencies) in projects.items():
        package_dir = root / "packages" / name
        package_dir.mkdir(parents=True)
        dependency_text = repr(list(dependencies))
        (package_dir / "pyproject.toml").write_text(
            "[project]\n"
            f'name = "{name}"\n'
            f'version = "{version}"\n'
            f"dependencies = {dependency_text}\n"
        )
    _run(root, "git", "init")
    _run(root, "git", "config", "user.email", "release@example.invalid")
    _run(root, "git", "config", "user.name", "Release Test")
    _run(root, "git", "add", "packages")
    _run(root, "git", "commit", "-m", "release candidates")
    return root


def test_dataframe_tool_packages_support_the_stable_pre_one_orchestrator_api() -> None:
    package_names = {
        "bioimageflow-common-tools",
        "bioimageflow-measurement-tools",
        "bioimageflow-spot-tools",
        "bioimageflow-tracking-tools",
    }
    packages = {
        package.name: package
        for package in discover_packages()
        if package.name in package_names
    }

    assert set(packages) == package_names
    for package in packages.values():
        dependencies = load_pyproject(package.directory / "pyproject.toml")["project"][
            "dependencies"
        ]
        [requirement] = [
            Requirement(text)
            for text in dependencies
            if Requirement(text).name == "bioimageflow"
        ]
        assert {
            (specifier.operator, specifier.version)
            for specifier in requirement.specifier
        } == {(">=", "0.1.6"), ("<", "1")}


def test_auto_selection_uses_only_pending_packages(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "current": ("1.0.0", ()),
            "historical": ("1.0.0", ()),
            "pending": ("1.1.0", ()),
            "unpublished": ("0.1.0", ()),
        },
    )
    _run(root, "git", "tag", "-a", "current-v1.0.0", "-m", "Release current")

    plan = select_release_set(
        root=root,
        remote_versions={
            "current": "1.0.0",
            "historical": "1.0.0",
            "pending": "1.0.0",
            "unpublished": None,
        },
    )

    assert [item.package.name for item in plan.items] == ["pending"]
    assert plan.payload()["release_tags"] == "pending-v1.1.0"

    explicit = select_release_set(
        ["unpublished"],
        root=root,
        remote_versions={"unpublished": None},
    )
    assert [item.package.name for item in explicit.items] == ["unpublished"]


def test_auto_selection_rejects_behind_and_changed_unbumped_packages(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "behind": ("1.0.0", ()),
            "changed": ("1.0.0", ()),
            "pending": ("1.1.0", ()),
        },
    )
    _run(root, "git", "tag", "-a", "changed-v1.0.0", "-m", "Release changed")
    changed = root / "packages" / "changed" / "module.py"
    changed.write_text("value = 1\n")
    _run(root, "git", "add", str(changed))
    _run(root, "git", "commit", "-m", "change published package")

    with pytest.raises(ReleaseError, match="behind.*bump-required"):
        select_release_set(
            root=root,
            remote_versions={
                "behind": "2.0.0",
                "changed": "1.0.0",
                "pending": "1.0.0",
            },
        )


def test_explicit_selection_accepts_exact_existing_tag_only(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"current": ("1.0.0", ()), "unknown": ("1.0.0", ())},
    )
    _run(root, "git", "tag", "-a", "current-v1.0.0", "-m", "Release current")

    result = tag_release_set(
        ["current"],
        root=root,
        remote_versions={"current": "1.0.0", "unknown": "1.0.0"},
        dry_run=True,
    )

    assert result.existing_tags == ("current-v1.0.0",)
    with pytest.raises(ReleaseError, match="unknown"):
        tag_release_set(
            ["unknown"],
            root=root,
            remote_versions={"current": "1.0.0", "unknown": "1.0.0"},
            dry_run=True,
        )


def test_unselected_workspace_dependency_must_be_available(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "demo-app": ("2.0.0", ("demo-core>=1.2.3,<2",)),
            "demo-core": ("1.2.3", ()),
        },
    )

    with pytest.raises(ReleaseError, match="include or bump"):
        select_release_set(
            ["demo-app"],
            root=root,
            remote_versions={"demo-app": "1.9.0", "demo-core": "1.1.0"},
        )

    plan = select_release_set(
        ["demo-app"],
        root=root,
        remote_versions={"demo-app": "1.9.0", "demo-core": "1.2.3"},
    )
    assert plan.publish_order == ("demo-app",)


def test_explicit_selection_queries_only_its_dependency_closure(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "demo-app": ("2.0.0", ("demo-core>=1.2.3,<2",)),
            "demo-core": ("1.2.3", ()),
            "unrelated": ("4.0.0", ()),
        },
    )
    queried: list[str] = []

    def load_version(name: str) -> str | None:
        queried.append(name)
        return {"demo-app": "1.9.0", "demo-core": "1.2.3"}[name]

    tag_release_set(
        ["demo-app"],
        root=root,
        version_loader=load_version,
        dry_run=True,
    )

    assert queried == ["demo-app", "demo-core"]


def test_dry_run_preflights_without_creating_tags(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})

    result = tag_release_set(
        root=root,
        remote_versions={"demo": "1.0.0"},
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.created_tags == ()
    assert not tag_exists(root, "demo-v1.1.0")


def test_tag_creation_is_annotated_bound_and_idempotent(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    versions = {"demo": "1.0.0"}

    created = tag_release_set(root=root, remote_versions=versions)
    repeated = tag_release_set(root=root, remote_versions=versions)

    assert created.created_tags == ("demo-v1.1.0",)
    assert created.payload()["release_tags"] == "demo-v1.1.0"
    assert git_output(root, "cat-file", "-t", "demo-v1.1.0") == "tag"
    assert git_output(root, "rev-list", "-n", "1", "demo-v1.1.0") == created.plan.sha
    assert repeated.created_tags == ()
    assert repeated.existing_tags == ("demo-v1.1.0",)


def test_conflicting_tag_aborts_before_other_tags_are_created(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"alpha": ("1.1.0", ()), "beta": ("1.1.0", ())},
    )
    first_sha = git_output(root, "rev-parse", "HEAD")
    marker = root / "marker"
    marker.write_text("next\n")
    _run(root, "git", "add", "marker")
    _run(root, "git", "commit", "-m", "advance head")
    _run(
        root,
        "git",
        "tag",
        "-a",
        "beta-v1.1.0",
        first_sha,
        "-m",
        "Wrong target",
    )

    with pytest.raises(ReleaseError, match="not HEAD"):
        tag_release_set(
            root=root,
            remote_versions={"alpha": "1.0.0", "beta": "1.0.0"},
        )

    assert not tag_exists(root, "alpha-v1.1.0")


def test_creation_failure_rolls_back_only_new_tags(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"alpha": ("1.1.0", ()), "beta": ("1.1.0", ())},
    )
    creations = 0

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal creations
        if command[:3] == ["git", "tag", "-a"]:
            creations += 1
            if creations == 2:
                return subprocess.CompletedProcess(command, 1, "", "tag failure")
        return subprocess.run(command, **kwargs)

    with pytest.raises(ReleaseError, match="tag failure"):
        tag_release_set(
            root=root,
            remote_versions={"alpha": "1.0.0", "beta": "1.0.0"},
            runner=runner,
        )

    assert not tag_exists(root, "alpha-v1.1.0")
    assert not tag_exists(root, "beta-v1.1.0")


def test_atomic_push_failure_retains_local_tags(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    _run(root, "git", "remote", "add", "origin", str(tmp_path / "remote.git"))
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["git", "push", "--atomic"]:
            return subprocess.CompletedProcess(command, 1, "", "remote rejected")
        return subprocess.run(command, **kwargs)

    with pytest.raises(ReleaseError, match="retained for an explicit retry") as caught:
        tag_release_set(
            root=root,
            remote_versions={"demo": "1.0.0"},
            push_remote="origin",
            runner=runner,
        )

    assert tag_exists(root, "demo-v1.1.0")
    assert "remote rejected" not in str(caught.value)
    push = next(command for command in commands if command[:2] == ["git", "push"])
    assert push == [
        "git",
        "push",
        "--atomic",
        "--",
        "origin",
        "refs/tags/demo-v1.1.0:refs/tags/demo-v1.1.0",
    ]


def test_atomic_push_succeeds_against_configured_bare_remote(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(root, "git", "remote", "add", "release", str(remote))

    result = tag_release_set(
        root=root,
        remote_versions={"demo": "1.0.0"},
        push_remote="release",
    )

    remote_target = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-list", "-n", "1", "demo-v1.1.0"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.pushed_to == "release"
    assert remote_target == result.plan.sha


def test_push_rejects_urls_without_echoing_credentials(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    secret_url = "https://token-value@example.invalid/repository.git"

    with pytest.raises(ReleaseError) as caught:
        tag_release_set(
            root=root,
            remote_versions={"demo": "1.0.0"},
            push_remote=secret_url,
        )

    assert str(caught.value) == "Push target must name a configured Git remote"
    assert secret_url not in str(caught.value)


def test_tag_cli_emits_the_json_release_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    result = tag_release_set(
        root=root,
        remote_versions={"demo": "1.0.0"},
        dry_run=True,
    )
    monkeypatch.setattr(
        release_set_module,
        "tag_release_set",
        lambda packages, *, dry_run, push_remote: result,
    )

    assert release_set_module.main(["tag", "--dry-run", "demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["release_tags"] == "demo-v1.1.0"
    assert payload["dry_run"] is True


def test_tagger_identity_and_pypi_queries_fail_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})
    original_git_output = release_set_module.git_output

    def no_identity(root: Path, *arguments: str) -> str:
        if arguments == ("var", "GIT_COMMITTER_IDENT"):
            raise ReleaseError("missing tagger identity")
        return original_git_output(root, *arguments)

    monkeypatch.setattr(release_set_module, "git_output", no_identity)
    with pytest.raises(ReleaseError, match="missing tagger identity"):
        tag_release_set(root=root, remote_versions={"demo": "1.0.0"})
    assert not tag_exists(root, "demo-v1.1.0")

    monkeypatch.setattr(release_set_module, "git_output", original_git_output)

    def unavailable(_: str) -> str | None:
        raise OSError("network unavailable")

    with pytest.raises(ReleaseError, match="Could not query PyPI"):
        tag_release_set(root=root, version_loader=unavailable)
    assert not tag_exists(root, "demo-v1.1.0")


def test_dry_run_cannot_push(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"demo": ("1.1.0", ())})

    with pytest.raises(ReleaseError, match="cannot be combined"):
        tag_release_set(
            root=root,
            remote_versions={"demo": "1.0.0"},
            dry_run=True,
            push_remote="origin",
        )
