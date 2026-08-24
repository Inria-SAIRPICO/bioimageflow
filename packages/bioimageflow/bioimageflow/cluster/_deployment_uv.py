"""Locked uv environment preparation helpers."""

from __future__ import annotations

import importlib.metadata
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from ._deployment_snapshot import (
    _canonical_distribution_name,
    _environment_failure,
    _load_toml,
    _require_directory,
    _stable_artifact,
)
from ._deployment_uv_runtime import _required_uv_runtime_identity
from .values import ClusterEnvironment

def _run_uv(
    executable: str,
    arguments: list[str],
    *,
    timeout: float = 120.0,
    failure_code: str = "environment-build-lock-incomplete",
) -> None:
    inherited_names = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "UV_CACHE_DIR")
    environment = {
        name: os.environ[name] for name in inherited_names if name in os.environ
    }
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": "315532800",
            "UV_OFFLINE": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    try:
        completed = subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _environment_failure(
            failure_code,
            "Offline uv preparation could not complete.",
        ) from exc
    if completed.returncode != 0:
        raise _environment_failure(
            failure_code,
            "Offline uv preparation failed; the lock or build closure is incomplete.",
        )


def _validate_uv_frozen(executable: str, project: Path) -> None:
    _run_uv(
        executable,
        [
            "lock",
            "--check",
            "--offline",
            "--no-progress",
            "--no-config",
            "--project",
            str(project),
        ],
        timeout=60.0,
        failure_code="environment-lock-invalid",
    )


def _current_bioimageflow_source() -> tuple[Path, str]:
    try:
        distribution = importlib.metadata.distribution("bioimageflow")
        version = distribution.version
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = (
            json.loads(direct_url_text) if direct_url_text is not None else None
        )
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError) as exc:
        raise _environment_failure(
            "environment-artifact-missing",
            "The exact running BioImageFlow distribution source is unavailable.",
        ) from exc
    if (
        type(direct_url) is not dict
        or type(direct_url.get("url")) is not str
        or not isinstance(direct_url.get("dir_info"), Mapping)
    ):
        raise _environment_failure(
            "environment-artifact-missing",
            "A non-editable running BioImageFlow installation requires its original wheel.",
        )
    parsed = urlparse(direct_url["url"])
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise _environment_failure(
            "environment-artifact-missing",
            "The exact running BioImageFlow source must be a local project.",
        )
    source = Path(unquote(parsed.path))
    try:
        _require_directory(source, description="The running BioImageFlow source")
    except (OSError, ValueError) as exc:
        raise _environment_failure(
            "environment-artifact-missing",
            "The running BioImageFlow source is unavailable.",
        ) from exc
    return source, version


def _build_local_distribution(
    executable: str,
    source: Path,
    destination: Path,
    *,
    expected_name: str,
    expected_version: str,
) -> dict[str, Any]:
    try:
        from packaging.utils import parse_sdist_filename, parse_wheel_filename
    except ModuleNotFoundError as exc:
        raise ImportError(
            "uv deployment preparation requires bioimageflow[cluster]."
        ) from exc
    try:
        _require_directory(source, description="A local distribution source")
    except (OSError, ValueError) as exc:
        raise _environment_failure(
            "environment-artifact-missing",
            "A selected local distribution source is unavailable.",
        ) from exc
    destination.mkdir(parents=True, exist_ok=False)
    common = [
        "--offline",
        "--no-progress",
        "--no-build-logs",
        "--no-create-gitignore",
        "--no-config",
        "--out-dir",
        str(destination),
    ]
    _run_uv(executable, ["build", "--sdist", *common, str(source)])
    sdists = tuple(destination.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise _environment_failure(
            "environment-artifact-missing",
            "The local build did not produce exactly one source distribution.",
        )
    sdist = sdists[0]
    _run_uv(executable, ["build", "--wheel", *common, str(sdist)])
    wheels = tuple(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise _environment_failure(
            "environment-artifact-missing",
            "The local build did not produce exactly one wheel.",
        )
    wheel = wheels[0]
    try:
        sdist_name, sdist_version = parse_sdist_filename(sdist.name)
        wheel_name, wheel_version, _build, wheel_tags = parse_wheel_filename(wheel.name)
    except ValueError as exc:
        raise _environment_failure(
            "environment-artifact-missing",
            "The local build produced a malformed distribution filename.",
        ) from exc
    canonical_expected = _canonical_distribution_name(expected_name)
    if (
        _canonical_distribution_name(str(sdist_name)) != canonical_expected
        or _canonical_distribution_name(str(wheel_name)) != canonical_expected
        or str(sdist_version) != expected_version
        or str(wheel_version) != expected_version
    ):
        raise _environment_failure(
            "environment-artifact-missing",
            "The local build artifact identity differs from the locked project.",
        )
    if any(
        tag.platform != "any"
        or tag.abi != "none"
        or not (tag.interpreter == "py3" or tag.interpreter.startswith("py2.py3"))
        for tag in wheel_tags
    ):
        raise _environment_failure(
            "environment-platform-incompatible",
            "A local project produced a target-specific wheel before cluster discovery.",
        )
    try:
        pyproject = _load_toml(
            source / "pyproject.toml", description="Local pyproject.toml"
        )
    except ValueError as exc:
        raise _environment_failure(
            "environment-build-lock-incomplete",
            "A local project build declaration is invalid.",
        ) from exc
    build_system = pyproject.get("build-system")
    if not isinstance(build_system, Mapping):
        raise _environment_failure(
            "environment-build-lock-incomplete",
            "A local project has no declared PEP 517 build system.",
        )
    backend = build_system.get("build-backend")
    requires = build_system.get("requires")
    backend_path = build_system.get("backend-path", [])
    if (
        type(backend) is not str
        or not isinstance(requires, list)
        or any(type(item) is not str for item in requires)
        or not isinstance(backend_path, list)
        or any(type(item) is not str for item in backend_path)
    ):
        raise _environment_failure(
            "environment-build-lock-incomplete",
            "A local project has an invalid PEP 517 build declaration.",
        )
    return {
        "name": canonical_expected,
        "version": expected_version,
        "build_backend": backend,
        "build_requires": list(requires),
        "backend_path": list(backend_path),
        "source_distribution": _stable_artifact(sdist),
        "wheel": {
            **_stable_artifact(wheel),
            "tags": sorted(str(tag) for tag in wheel_tags),
        },
    }


def _uv_package_dependencies(
    package: Mapping[str, Any],
    *,
    groups: tuple[str, ...],
    extras: tuple[str, ...],
) -> tuple[str, ...]:
    values: list[Any] = []
    dependencies = package.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise _environment_failure(
            "environment-lock-invalid",
            "A uv lock package has malformed dependencies.",
        )
    values.extend(dependencies)
    optional = package.get("optional-dependencies", {})
    development = package.get("dev-dependencies", {})
    metadata = package.get("metadata", {})
    metadata_development = (
        metadata.get("requires-dev", {}) if isinstance(metadata, Mapping) else {}
    )
    if (
        not isinstance(optional, Mapping)
        or not isinstance(development, Mapping)
        or not isinstance(metadata_development, Mapping)
    ):
        raise _environment_failure(
            "environment-lock-invalid",
            "A uv lock package has malformed selected dependency tables.",
        )
    for selection, table, label in (
        (extras, optional, "extra"),
        (groups, development, "group"),
    ):
        for name in selection:
            selected = table.get(name)
            if label == "group" and selected is None and name in metadata_development:
                selected = []
            if not isinstance(selected, list):
                raise _environment_failure(
                    "environment-lock-invalid",
                    f"The selected uv {label} {name!r} is absent from the lock.",
                )
            values.extend(selected)
    result: list[str] = []
    for dependency in values:
        if (
            not isinstance(dependency, Mapping)
            or type(dependency.get("name")) is not str
        ):
            raise _environment_failure(
                "environment-lock-invalid",
                "A uv lock dependency reference is malformed.",
            )
        result.append(_canonical_distribution_name(dependency["name"]))
    return tuple(result)


def _safe_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _environment_failure(
            "environment-lock-invalid",
            "A uv lock contains a non-canonical or credential-bearing endpoint.",
        )
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _locked_artifacts(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    raw_values: list[Any] = []
    wheels = package.get("wheels", [])
    if not isinstance(wheels, list):
        raise _environment_failure(
            "environment-lock-invalid",
            "A uv lock package has a malformed wheel list.",
        )
    raw_values.extend(wheels)
    for artifact in raw_values:
        if (
            not isinstance(artifact, Mapping)
            or type(artifact.get("url")) is not str
            or type(artifact.get("hash")) is not str
            or type(artifact.get("size")) is not int
            or artifact["size"] <= 0
            or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["hash"]) is None
        ):
            raise _environment_failure(
                "environment-lock-invalid",
                "Every uv registry artifact requires a URL, size, and SHA-256 hash.",
            )
        url = _safe_endpoint(artifact["url"])
        artifacts.append(
            {
                "filename": Path(unquote(urlparse(url).path)).name,
                "size": artifact["size"],
                "digest": artifact["hash"],
                "endpoint": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                "url": url,
            }
        )
    return artifacts


def _capture_locked_artifact(
    artifact: Mapping[str, Any], destination: Path
) -> dict[str, Any]:
    """Capture one registry wheel while enforcing its lock identity."""
    expected_size = artifact["size"]
    expected_digest = artifact["digest"]
    url = artifact["url"]
    if (
        type(expected_size) is not int
        or type(expected_digest) is not str
        or type(url) is not str
    ):
        raise _environment_failure(
            "environment-lock-invalid", "A locked wheel identity is malformed."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        request = Request(url, headers={"User-Agent": "BioImageFlow locked-uv capture"})
        with urlopen(request, timeout=60.0) as response, destination.open("xb") as stream:
            final_url = _safe_endpoint(response.geturl())
            if final_url != url:
                raise _environment_failure(
                    "environment-artifact-missing",
                    "A locked wheel redirected to a different immutable identity.",
                )
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise _environment_failure(
                        "environment-artifact-missing",
                        "A locked wheel has a different size than uv.lock.",
                    )
                digest.update(chunk)
                stream.write(chunk)
    except ValueError:
        raise
    except (OSError, TimeoutError) as exc:
        raise _environment_failure(
            "environment-artifact-missing",
            "A locked wheel could not be captured before network submission.",
        ) from exc
    observed_digest = f"sha256:{digest.hexdigest()}"
    if size != expected_size or observed_digest != expected_digest:
        raise _environment_failure(
            "environment-artifact-missing",
            "A captured wheel differs from its uv.lock hash or size.",
        )
    return {
        "filename": destination.name,
        "size": size,
        "digest": observed_digest,
    }


def _capture_uv_installer_artifacts(
    version: str, content_root: Path
) -> list[dict[str, Any]]:
    """Capture pinned non-Windows uv wheels for target-side artifact selection."""
    try:
        from packaging.utils import parse_wheel_filename
    except ModuleNotFoundError as exc:
        raise ImportError(
            "uv deployment preparation requires bioimageflow[cluster]."
        ) from exc
    endpoint = _safe_endpoint(f"https://pypi.org/pypi/uv/{version}/json")
    try:
        with urlopen(
            Request(endpoint, headers={"User-Agent": "BioImageFlow locked-uv capture"}),
            timeout=30.0,
        ) as response:
            encoded = response.read(4 * 1024 * 1024 + 1)
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("installer metadata is too large")
        metadata = json.loads(encoded)
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _environment_failure(
            "environment-installer-unavailable",
            "Pinned uv installer metadata could not be captured.",
        ) from exc
    files = metadata.get("urls") if isinstance(metadata, Mapping) else None
    if not isinstance(files, list):
        raise _environment_failure(
            "environment-installer-unavailable", "Pinned uv installer metadata is invalid."
        )
    candidates: list[Mapping[str, Any]] = []
    for item in files:
        if not isinstance(item, Mapping) or item.get("packagetype") != "bdist_wheel":
            continue
        filename = item.get("filename")
        if type(filename) is not str:
            continue
        try:
            name, wheel_version, _build, tags = parse_wheel_filename(filename)
        except ValueError:
            continue
        if _canonical_distribution_name(str(name)) != "uv" or str(wheel_version) != version:
            continue
        # The managed gateway is POSIX-only. Capture every published POSIX target
        # so target selection remains remote and no laptop ABI is assumed.
        if any(tag.platform.startswith("win") for tag in tags):
            continue
        digest_table = item.get("digests")
        sha256 = digest_table.get("sha256") if isinstance(digest_table, Mapping) else None
        if (
            type(item.get("url")) is not str
            or type(item.get("size")) is not int
            or type(sha256) is not str
        ):
            continue
        candidates.append(
            {
                "filename": filename,
                "size": item["size"],
                "digest": f"sha256:{sha256}",
                "url": _safe_endpoint(item["url"]),
                "endpoint": f"{urlparse(item['url']).scheme}://{urlparse(item['url']).netloc}",
                "tags": sorted(str(tag) for tag in tags),
            }
        )
    if not candidates:
        raise _environment_failure(
            "environment-installer-unavailable",
            "No pinned POSIX uv installer wheel is available.",
        )
    captured: list[dict[str, Any]] = []
    for index, candidate in enumerate(sorted(candidates, key=lambda item: item["filename"])):
        relative = Path("environment") / "installers" / str(index) / candidate["filename"]
        artifact = _capture_locked_artifact(candidate, content_root / relative)
        captured.append(
            {
                **artifact,
                "path": relative.as_posix(),
                "endpoint": candidate["endpoint"],
                "tags": candidate["tags"],
            }
        )
    return captured


def _local_source_path(
    project: Path, package: Mapping[str, Any]
) -> tuple[str, Path | None]:
    source = package.get("source")
    if not isinstance(source, Mapping) or len(source) != 1:
        raise _environment_failure(
            "environment-lock-invalid",
            "A uv lock package has an invalid source.",
        )
    kind, value = next(iter(source.items()))
    if kind == "registry":
        if type(value) is not str:
            raise _environment_failure(
                "environment-lock-invalid", "A uv registry identity is invalid."
            )
        _safe_endpoint(value)
        return "registry", None
    if kind in {"editable", "directory", "virtual"}:
        if type(value) is not str:
            raise _environment_failure(
                "environment-lock-invalid", "A uv local source path is invalid."
            )
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise _environment_failure(
                "environment-lock-invalid",
                "A uv local source must remain inside the selected project.",
            )
        resolved = (project / relative).resolve()
        project_resolved = project.resolve()
        if resolved != project_resolved and project_resolved not in resolved.parents:
            raise _environment_failure(
                "environment-lock-invalid",
                "A uv local source escapes the selected project.",
            )
        return str(kind), resolved
    if kind in {"git", "url"}:
        if type(value) is not str:
            raise _environment_failure(
                "environment-lock-invalid", "A uv immutable source is invalid."
            )
        return str(kind), None
    raise _environment_failure(
        "environment-lock-invalid", "A uv lock package uses an unsupported source kind."
    )


def _prepare_uv_environment(
    environment: ClusterEnvironment,
    *,
    project: Path,
    captured_pyproject: Path,
    captured_lock: Path,
    content_root: Path,
    uv_executable: str,
    uv_version: str,
    scheduler: str,
) -> dict[str, Any]:
    try:
        lock = _load_toml(captured_lock, description="uv.lock")
        manifest = _load_toml(captured_pyproject, description="uv pyproject.toml")
    except ValueError as exc:
        raise _environment_failure(
            "environment-lock-invalid",
            "The captured uv project or lock is malformed.",
        ) from exc
    if type(lock.get("version")) is not int or lock["version"] < 1:
        raise _environment_failure(
            "environment-lock-invalid", "uv.lock has an unsupported format version."
        )
    packages = lock.get("package")
    if not isinstance(packages, list) or not packages:
        raise _environment_failure(
            "environment-lock-invalid", "uv.lock contains no package resolution."
        )
    normalized: list[Mapping[str, Any]] = []
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for package in packages:
        if (
            not isinstance(package, Mapping)
            or type(package.get("name")) is not str
            or type(package.get("version")) is not str
        ):
            raise _environment_failure(
                "environment-lock-invalid", "uv.lock contains a malformed package."
            )
        normalized.append(package)
        by_name.setdefault(_canonical_distribution_name(package["name"]), []).append(
            package
        )
    project_table = manifest.get("project")
    if (
        not isinstance(project_table, Mapping)
        or type(project_table.get("name")) is not str
    ):
        raise _environment_failure(
            "environment-lock-invalid", "The uv project has no static project name."
        )
    selected_name = _canonical_distribution_name(
        environment.package or project_table["name"]
    )
    selected_candidates = by_name.get(selected_name, [])
    if not selected_candidates:
        raise _environment_failure(
            "environment-lock-invalid",
            "The selected uv package is absent from uv.lock.",
        )
    try:
        running_version = importlib.metadata.version("bioimageflow")
    except importlib.metadata.PackageNotFoundError as exc:
        raise _environment_failure(
            "environment-artifact-missing", "BioImageFlow is not installed locally."
        ) from exc
    locked_bioimageflow = by_name.get("bioimageflow", [])
    if any(package["version"] != running_version for package in locked_bioimageflow):
        raise _environment_failure(
            "bioimageflow-version-conflict",
            "uv.lock selects a different BioImageFlow version than the running client.",
        )
    reachable_names = {selected_name}
    pending = list(selected_candidates)
    seen_ids: set[int] = set()
    while pending:
        package = pending.pop()
        if id(package) in seen_ids:
            continue
        seen_ids.add(id(package))
        selected_root = _canonical_distribution_name(package["name"]) == selected_name
        dependencies = _uv_package_dependencies(
            package,
            groups=environment.groups if selected_root else (),
            extras=environment.extras if selected_root else (),
        )
        for dependency_name in dependencies:
            candidates = by_name.get(dependency_name)
            if not candidates:
                raise _environment_failure(
                    "environment-lock-invalid",
                    f"uv.lock omits dependency {dependency_name!r}.",
                )
            reachable_names.add(dependency_name)
            pending.extend(candidates)
    scheduler_plugin = _required_uv_runtime_identity(
        by_name, reachable_names, scheduler=scheduler
    )
    locked_plan: list[dict[str, Any]] = []
    local_builds: list[tuple[Mapping[str, Any], Path]] = []
    endpoint_identities: set[str] = set()
    for package_index, package in enumerate(normalized):
        name = _canonical_distribution_name(package["name"])
        if name not in reachable_names:
            continue
        source_kind, local_path = _local_source_path(project, package)
        source = package["source"]
        source_identity: str | None = None
        artifacts: list[dict[str, Any]] = []
        if source_kind == "registry":
            source_identity = _safe_endpoint(source["registry"])
            endpoint_identities.add(source_identity)
            artifacts = _locked_artifacts(package)
            if not artifacts:
                raise _environment_failure(
                    "environment-build-lock-incomplete",
                    f"Registry package {name!r} has no hashed wheel.",
                )
            endpoint_identities.update(item["endpoint"] for item in artifacts)
            captured_artifacts: list[dict[str, Any]] = []
            for artifact in artifacts:
                filename = artifact["filename"]
                if (
                    type(filename) is not str
                    or not filename.endswith(".whl")
                    or filename in {"", ".", ".."}
                ):
                    raise _environment_failure(
                        "environment-lock-invalid",
                        "A locked registry wheel has an invalid filename.",
                    )
                relative = Path("environment") / "registry" / str(package_index) / filename
                captured = _capture_locked_artifact(artifact, content_root / relative)
                captured_artifacts.append(
                    {
                        **captured,
                        "endpoint": artifact["endpoint"],
                        "path": relative.as_posix(),
                    }
                )
            artifacts = captured_artifacts
        elif source_kind in {"editable", "directory"} and name != "bioimageflow":
            assert local_path is not None
            local_builds.append((package, local_path))
        locked_plan.append(
            {
                "name": name,
                "version": package["version"],
                "source_kind": source_kind,
                "source_identity": source_identity,
                "artifacts": artifacts,
            }
        )
    artifact_root = content_root / "environment" / "artifacts"
    bioimageflow_source, authoritative_version = _current_bioimageflow_source()
    if authoritative_version != running_version:
        raise _environment_failure(
            "bioimageflow-version-conflict",
            "The running BioImageFlow source and distribution versions differ.",
        )
    bioimageflow_build = _build_local_distribution(
        uv_executable,
        bioimageflow_source,
        artifact_root / "bioimageflow",
        expected_name="bioimageflow",
        expected_version=running_version,
    )
    bioimageflow_build["wheel"]["path"] = (
        Path("environment")
        / "artifacts"
        / "bioimageflow"
        / bioimageflow_build["wheel"]["filename"]
    ).as_posix()
    local_artifacts = [bioimageflow_build]
    for index, (package, source) in enumerate(local_builds, start=1):
        local_build = _build_local_distribution(
            uv_executable,
            source,
            artifact_root / str(index),
            expected_name=package["name"],
            expected_version=package["version"],
        )
        local_build["wheel"]["path"] = (
            Path("environment")
            / "artifacts"
            / str(index)
            / local_build["wheel"]["filename"]
        ).as_posix()
        local_artifacts.append(local_build)
    install_arguments = ["sync", "--frozen", "--no-editable", "--no-python-downloads"]
    for group in environment.groups:
        install_arguments.extend(("--group", group))
    for extra in environment.extras:
        install_arguments.extend(("--extra", extra))
    if environment.package is not None:
        install_arguments.extend(("--package", environment.package))
    installer_artifacts = _capture_uv_installer_artifacts(uv_version, content_root)
    return {
        "schema": "bioimageflow.uv_install_plan.v1",
        "installer": {
            "name": "uv",
            "version": uv_version,
            "artifacts": installer_artifacts,
        },
        "frozen": True,
        "lock_version": lock["version"],
        "lock_revision": lock.get("revision"),
        "project_manifest": _stable_artifact(captured_pyproject),
        "lock": _stable_artifact(captured_lock),
        "requires_python": lock.get("requires-python"),
        "selected_package": selected_name,
        "groups": list(environment.groups),
        "extras": list(environment.extras),
        "auth_refs": dict(environment.auth_refs),
        "psij_scheduler_plugin": scheduler_plugin,
        "endpoint_identities": sorted(endpoint_identities),
        "locked_packages": sorted(
            locked_plan,
            key=lambda item: (item["name"], item["version"], item["source_kind"]),
        ),
        "local_artifacts": local_artifacts,
        "target_policy": "captured-wheels-target-selected",
        "install_arguments": install_arguments,
        "network_resolution": False,
    }



__all__ = ["_prepare_uv_environment", "_validate_uv_frozen"]
