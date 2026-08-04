"""Discover, tag, validate, publish, and verify coordinated package releases."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_support import (  # noqa: E402
    PYPI_JSON_BASE_URL,
    Package,
    ReleaseError,
    classify_package_release,
    discover_packages,
    git_output,
    load_pyproject,
    parse_release_tag,
    pypi_version,
    tag_exists,
    validate_release_artifacts,
)


PYPI_SIMPLE_URL = "https://pypi.org/simple"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
VersionLoader = Callable[[str], str | None]
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ReleaseItem:
    package: Package
    version: str
    tag: str

    def payload(self) -> dict[str, str]:
        return {
            "package": self.package.name,
            "normalized_name": self.package.normalized_name,
            "version": self.version,
            "tag": self.tag,
        }


@dataclass(frozen=True)
class ReleasePlan:
    sha: str
    items: tuple[ReleaseItem, ...]
    publish_order: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "items": [item.payload() for item in self.items],
            "publish_order": list(self.publish_order),
            "release_tags": " ".join(item.tag for item in self.items),
        }


@dataclass(frozen=True)
class TagResult:
    """Result of validating, creating, and optionally pushing release tags."""

    plan: ReleasePlan
    created_tags: tuple[str, ...]
    existing_tags: tuple[str, ...]
    pushed_to: str | None
    dry_run: bool

    def payload(self) -> dict[str, object]:
        return {
            **self.plan.payload(),
            "created_tags": list(self.created_tags),
            "existing_tags": list(self.existing_tags),
            "pushed_to": self.pushed_to,
            "dry_run": self.dry_run,
        }


def _selected_dependencies(
    items: Sequence[ReleaseItem],
    workspace_packages: Sequence[Package],
    *,
    remote_versions: Mapping[str, str | None] | None = None,
) -> dict[str, set[str]]:
    by_canonical_name = {
        canonicalize_name(item.package.name): item
        for item in items
    }
    workspace_by_name = {
        canonicalize_name(package.name): package
        for package in workspace_packages
    }
    dependencies = {item.package.name: set() for item in items}
    for item in items:
        project = load_pyproject(item.package.directory / "pyproject.toml")["project"]
        for dependency_text in project.get("dependencies", []):
            try:
                requirement = Requirement(dependency_text)
            except InvalidRequirement as error:
                raise ReleaseError(
                    f"Invalid dependency for {item.package.name}: {dependency_text}"
                ) from error
            canonical_name = canonicalize_name(requirement.name)
            workspace_dependency = workspace_by_name.get(canonical_name)
            if workspace_dependency is None:
                continue
            dependency = by_canonical_name.get(canonical_name)
            if dependency is None:
                if remote_versions is None:
                    continue
                remote_text = remote_versions.get(workspace_dependency.name)
                if remote_text is None:
                    raise ReleaseError(
                        f"{item.package.name} requires {dependency_text!r}, but "
                        f"{workspace_dependency.name} is unavailable on PyPI; "
                        "include or release that dependency"
                    )
                try:
                    compatible = requirement.specifier.contains(
                        Version(remote_text),
                        prereleases=True,
                    )
                except InvalidVersion as error:
                    raise ReleaseError(
                        f"Invalid PyPI version for {workspace_dependency.name}: "
                        f"{remote_text}"
                    ) from error
                if not compatible:
                    raise ReleaseError(
                        f"{item.package.name} dependency {dependency_text!r} does not "
                        f"accept available {workspace_dependency.name}=={remote_text}; "
                        "include or bump that dependency"
                    )
                continue
            try:
                compatible = requirement.specifier.contains(
                    Version(dependency.version),
                    prereleases=True,
                )
            except InvalidVersion as error:
                raise ReleaseError(
                    f"Invalid selected version for {dependency.package.name}: {dependency.version}"
                ) from error
            if not compatible:
                raise ReleaseError(
                    f"{item.package.name} dependency {dependency_text!r} does not accept "
                    f"selected {dependency.package.name}=={dependency.version}"
                )
            dependencies[item.package.name].add(dependency.package.name)
    return dependencies


def _publish_order(
    items: Sequence[ReleaseItem],
    workspace_packages: Sequence[Package],
    *,
    remote_versions: Mapping[str, str | None] | None = None,
) -> tuple[str, ...]:
    dependencies = _selected_dependencies(
        items,
        workspace_packages,
        remote_versions=remote_versions,
    )
    remaining = {name: set(values) for name, values in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(name for name, values in remaining.items() if not values)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ReleaseError(f"Selected package dependency cycle: {cycle}")
        ordered.extend(ready)
        for name in ready:
            remaining.pop(name)
        for values in remaining.values():
            values.difference_update(ready)
    return tuple(ordered)


def _build_release_plan(
    items: Sequence[ReleaseItem],
    sha: str,
    workspace_packages: Sequence[Package],
    *,
    remote_versions: Mapping[str, str | None] | None = None,
) -> ReleasePlan:
    ordered_items = tuple(sorted(items, key=lambda item: item.package.name))
    if remote_versions is not None:
        for item in ordered_items:
            remote_text = remote_versions.get(item.package.name)
            if remote_text is None:
                continue
            try:
                remote = Version(remote_text)
                requested = Version(item.version)
            except InvalidVersion as error:
                raise ReleaseError(f"Invalid package version: {error}") from error
            if remote > requested:
                raise ReleaseError(
                    f"PyPI already has newer {item.package.name}=={remote_text}; "
                    f"refusing release {item.version}"
                )
    return ReleasePlan(
        sha=sha,
        items=ordered_items,
        publish_order=_publish_order(
            ordered_items,
            workspace_packages,
            remote_versions=remote_versions,
        ),
    )


def validate_release_set(
    tags: Sequence[str],
    *,
    root: Path = ROOT,
    remote_versions: Mapping[str, str | None] | None = None,
    require_clean: bool = True,
) -> ReleasePlan:
    if not tags:
        raise ReleaseError("At least one release tag is required")

    packages = discover_packages(root)
    items: list[ReleaseItem] = []
    commits: set[str] = set()
    selected_names: set[str] = set()
    for tag in tags:
        package, version = parse_release_tag(tag, packages)
        if package.name in selected_names:
            raise ReleaseError(f"Package selected more than once: {package.name}")
        selected_names.add(package.name)
        if package.version != version:
            raise ReleaseError(
                f"Tag requests {package.name}=={version}, but pyproject.toml declares "
                f"{package.version}"
            )
        if not tag_exists(root, tag):
            raise ReleaseError(f"Release tag does not exist locally: {tag}")
        if git_output(root, "cat-file", "-t", tag) != "tag":
            raise ReleaseError(f"Release tag must be annotated: {tag}")
        commits.add(git_output(root, "rev-list", "-n", "1", tag))
        items.append(ReleaseItem(package=package, version=version, tag=tag))

    if len(commits) != 1:
        raise ReleaseError("Every release tag must point to the same commit")
    [sha] = commits
    if sha != git_output(root, "rev-parse", "HEAD"):
        raise ReleaseError("Every release tag must point to HEAD")
    if require_clean and git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Working tree must be clean before release validation")

    return _build_release_plan(
        items,
        sha,
        packages,
        remote_versions=remote_versions,
    )


def fetch_package_versions(
    packages: Sequence[Package],
    *,
    loader: VersionLoader | None = None,
) -> dict[str, str | None]:
    """Fetch current PyPI versions for workspace dependency validation."""
    load_version = pypi_version if loader is None else loader
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package.name] = load_version(package.name)
        except Exception as error:
            raise ReleaseError(f"Could not query PyPI for {package.name}: {error}") from error
    return versions


def select_release_set(
    package_names: Sequence[str] = (),
    *,
    root: Path = ROOT,
    remote_versions: Mapping[str, str | None],
) -> ReleasePlan:
    """Select a validated release set from package names or workspace status."""
    if git_output(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Working tree must be clean before creating release tags")
    packages = discover_packages(root)
    by_name = {package.name: package for package in packages}
    explicit = bool(package_names)
    if len(set(package_names)) != len(package_names):
        raise ReleaseError("A release package was selected more than once")
    unknown_names = sorted(set(package_names).difference(by_name))
    if unknown_names:
        raise ReleaseError(f"Unknown release package: {', '.join(unknown_names)}")

    selected: list[Package] = []
    failures: list[str] = []
    candidates = [by_name[name] for name in package_names] if explicit else packages
    for package in candidates:
        if package.name not in remote_versions:
            failures.append(f"{package.name}: PyPI status was not provided")
            continue
        try:
            status = classify_package_release(
                package,
                remote_versions[package.name],
                root,
            )
        except (InvalidVersion, ValueError) as error:
            failures.append(f"{package.name}: {error}")
            continue
        if status.state == "pending" or (explicit and status.state == "unpublished"):
            selected.append(package)
        elif explicit and status.state == "up-to-date":
            selected.append(package)
        elif not explicit and status.state in {"unpublished", "up-to-date", "unknown"}:
            continue
        else:
            failures.append(f"{package.name}: {status.state}: {status.detail}")
    if failures:
        raise ReleaseError("Release selection failed: " + "; ".join(failures))
    if not selected:
        raise ReleaseError("No pending packages were selected")

    sha = git_output(root, "rev-parse", "HEAD")
    items = [
        ReleaseItem(package=package, version=package.version, tag=package.release_tag)
        for package in selected
    ]
    return _build_release_plan(
        items,
        sha,
        packages,
        remote_versions=remote_versions,
    )


def _preflight_release_tags(
    plan: ReleasePlan,
    *,
    root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    git_output(root, "var", "GIT_COMMITTER_IDENT")
    existing: list[str] = []
    missing: list[str] = []
    for item in plan.items:
        if not tag_exists(root, item.tag):
            missing.append(item.tag)
            continue
        if git_output(root, "cat-file", "-t", item.tag) != "tag":
            raise ReleaseError(f"Existing release tag must be annotated: {item.tag}")
        target = git_output(root, "rev-list", "-n", "1", item.tag)
        if target != plan.sha:
            raise ReleaseError(
                f"Existing release tag {item.tag} points to {target}, not HEAD {plan.sha}"
            )
        existing.append(item.tag)
    return tuple(existing), tuple(missing)


def _command_failure(command: Sequence[str], result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    rendered = " ".join(command)
    return f"{rendered} failed" + (f": {detail}" if detail else "")


def _version_query_packages(
    package_names: Sequence[str],
    packages: Sequence[Package],
) -> tuple[Package, ...]:
    """Return explicit packages and their recursive workspace dependencies."""
    if not package_names:
        return tuple(packages)
    by_name = {package.name: package for package in packages}
    unknown_names = sorted(set(package_names).difference(by_name))
    if unknown_names:
        raise ReleaseError(f"Unknown release package: {', '.join(unknown_names)}")
    by_canonical_name = {
        canonicalize_name(package.name): package
        for package in packages
    }
    selected_names = set(package_names)
    pending = list(package_names)
    while pending:
        name = pending.pop()
        project = load_pyproject(by_name[name].directory / "pyproject.toml")["project"]
        for dependency_text in project.get("dependencies", []):
            try:
                requirement = Requirement(dependency_text)
            except InvalidRequirement as error:
                raise ReleaseError(
                    f"Invalid dependency for {name}: {dependency_text}"
                ) from error
            dependency = by_canonical_name.get(canonicalize_name(requirement.name))
            if dependency is None or dependency.name in selected_names:
                continue
            selected_names.add(dependency.name)
            pending.append(dependency.name)
    return tuple(by_name[name] for name in sorted(selected_names))


def tag_release_set(
    package_names: Sequence[str] = (),
    *,
    root: Path = ROOT,
    remote_versions: Mapping[str, str | None] | None = None,
    version_loader: VersionLoader | None = None,
    dry_run: bool = False,
    push_remote: str | None = None,
    runner: CommandRunner | None = None,
) -> TagResult:
    """Validate, annotate, and optionally atomically push one release set."""
    if dry_run and push_remote is not None:
        raise ReleaseError("--dry-run cannot be combined with --push")
    if push_remote is not None and _REMOTE_NAME.fullmatch(push_remote) is None:
        raise ReleaseError("Push target must name a configured Git remote")
    packages = discover_packages(root)
    query_packages = _version_query_packages(package_names, packages)
    versions = (
        fetch_package_versions(query_packages, loader=version_loader)
        if remote_versions is None
        else dict(remote_versions)
    )
    plan = select_release_set(
        package_names,
        root=root,
        remote_versions=versions,
    )
    existing, missing = _preflight_release_tags(plan, root=root)
    if push_remote is not None:
        configured_remotes = set(git_output(root, "remote").splitlines())
        if push_remote not in configured_remotes:
            raise ReleaseError("Push target must name a configured Git remote")
        git_output(root, "remote", "get-url", push_remote)
    if dry_run:
        return TagResult(plan, (), existing, None, True)

    run = subprocess.run if runner is None else runner
    created: list[str] = []
    for tag in missing:
        item = next(item for item in plan.items if item.tag == tag)
        command = [
            "git",
            "tag",
            "-a",
            tag,
            plan.sha,
            "-m",
            f"Release {item.package.name} {item.version}",
        ]
        result = run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            created.append(tag)
            continue
        rollback_failures: list[str] = []
        for created_tag in reversed(created):
            rollback = run(
                ["git", "tag", "-d", created_tag],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            if rollback.returncode != 0:
                rollback_failures.append(created_tag)
        suffix = (
            f"; could not roll back {', '.join(rollback_failures)}"
            if rollback_failures
            else ""
        )
        raise ReleaseError(_command_failure(command, result) + suffix)

    if push_remote is not None:
        command = [
            "git",
            "push",
            "--atomic",
            "--",
            push_remote,
            *(f"refs/tags/{item.tag}:refs/tags/{item.tag}" for item in plan.items),
        ]
        result = run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ReleaseError(
                f"Atomic push to configured remote {push_remote!r} failed; "
                "validated local tags were retained for an explicit retry"
            )
    return TagResult(
        plan,
        tuple(created),
        existing,
        push_remote,
        False,
    )


def publish_release_set(
    plan: ReleasePlan,
    artifact_root: Path,
    *,
    runner: CommandRunner | None = None,
) -> list[str]:
    run = subprocess.run if runner is None else runner
    by_name = {item.package.name: item for item in plan.items}
    published: list[str] = []
    for package_name in plan.publish_order:
        item = by_name[package_name]
        artifact_dir = artifact_root / f"release-{item.package.name}-{item.version}"
        artifacts = validate_release_artifacts(
            artifact_dir,
            item.package,
            item.version,
        )
        run(
            [
                "uv",
                "publish",
                "--trusted-publishing",
                "always",
                "--check-url",
                PYPI_SIMPLE_URL,
                *(str(path) for path in artifacts),
            ],
            cwd=ROOT,
            check=True,
            text=True,
        )
        published.append(package_name)
    return published


def verify_release_set(
    plan: ReleasePlan,
    *,
    attempts: int = 12,
    interval: float = 5.0,
) -> None:
    pending = {item.package.name: item.version for item in plan.items}
    for attempt in range(attempts):
        for package_name, version in list(pending.items()):
            if pypi_version(package_name, base_url=PYPI_JSON_BASE_URL) == version:
                pending.pop(package_name)
        if not pending:
            return
        if attempt + 1 < attempts:
            time.sleep(interval)
    detail = ", ".join(f"{name}=={version}" for name, version in sorted(pending.items()))
    raise ReleaseError(f"Published versions did not become visible on PyPI: {detail}")


def _plan_from_args(args: argparse.Namespace) -> ReleasePlan:
    plan = validate_release_set(args.tags, require_clean=not args.allow_dirty)
    if args.check_pypi:
        packages = discover_packages()
        plan = validate_release_set(
            args.tags,
            remote_versions=fetch_package_versions(packages),
            require_clean=not args.allow_dirty,
        )
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "publish", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("tags", nargs="+")
        command_parser.add_argument("--allow-dirty", action="store_true")
        command_parser.add_argument("--check-pypi", action="store_true")
        if command == "publish":
            command_parser.add_argument("--artifacts-dir", type=Path, required=True)
        if command == "verify":
            command_parser.add_argument("--attempts", type=int, default=12)
            command_parser.add_argument("--interval", type=float, default=5.0)
    tag_parser = subparsers.add_parser(
        "tag",
        help="discover and create one validated set of annotated release tags",
    )
    tag_parser.add_argument("packages", nargs="*")
    tag_parser.add_argument("--dry-run", action="store_true")
    tag_parser.add_argument(
        "--push",
        metavar="REMOTE",
        help="atomically push every validated tag to this configured Git remote",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "tag":
            result = tag_release_set(
                args.packages,
                dry_run=args.dry_run,
                push_remote=args.push,
            )
            print(json.dumps(result.payload(), separators=(",", ":")))
            return 0
        plan = _plan_from_args(args)
        if args.command == "plan":
            print(json.dumps(plan.payload(), separators=(",", ":")))
        elif args.command == "publish":
            published = publish_release_set(plan, args.artifacts_dir)
            print(f"Published {len(published)} package(s): {', '.join(published)}")
        else:
            verify_release_set(plan, attempts=args.attempts, interval=args.interval)
            print("Every coordinated release is visible on PyPI.")
    except (ReleaseError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
