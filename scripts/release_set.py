"""Validate, publish, and verify a coordinated set of package releases."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
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
        }


def _selected_dependencies(items: Sequence[ReleaseItem]) -> dict[str, set[str]]:
    by_canonical_name = {
        canonicalize_name(item.package.name): item
        for item in items
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
            dependency = by_canonical_name.get(canonicalize_name(requirement.name))
            if dependency is None:
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


def _publish_order(items: Sequence[ReleaseItem]) -> tuple[str, ...]:
    dependencies = _selected_dependencies(items)
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

    if remote_versions is not None:
        for item in items:
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

    items.sort(key=lambda item: item.package.name)
    return ReleasePlan(
        sha=sha,
        items=tuple(items),
        publish_order=_publish_order(items),
    )


def fetch_release_versions(items: Sequence[ReleaseItem]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for item in items:
        try:
            versions[item.package.name] = pypi_version(item.package.name)
        except Exception as error:
            raise ReleaseError(f"Could not query PyPI for {item.package.name}: {error}") from error
    return versions


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
        plan = validate_release_set(
            args.tags,
            remote_versions=fetch_release_versions(plan.items),
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
    args = parser.parse_args(argv)

    try:
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
