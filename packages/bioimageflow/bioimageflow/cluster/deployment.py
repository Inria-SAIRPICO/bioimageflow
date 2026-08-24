"""Immutable laptop-side preparation for managed cluster deployments."""

from __future__ import annotations

import importlib.metadata
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bioimageflow.launcher.cluster_bundle import _manifest
from bioimageflow.storage import canonical_json_bytes

from ._common import canonical_digest, thaw_json
from ._deployment_snapshot import (
    _capture_project_inputs,
    _copy_regular,
    _copy_tree,
    _environment_files,
    _environment_identity,
    _freeze_plain_json,
    _parsl_identity,
    _uv_executable_and_version,
    _validate_wheelhouse_files,
    _verify_snapshot,
)
from ._deployment_uv import (  # noqa: F401 - retained private test seam
    _build_local_distribution,
    _prepare_uv_environment,
    _validate_uv_frozen,
)
from .values import RemoteClusterConfig

DEPLOYMENT_MANIFEST_SCHEMA = "bioimageflow.cluster_deployment_manifest.v1"


@dataclass(frozen=True, slots=True)
class PreparedDeployment:
    """Private local source snapshot for one deployment publication."""

    root: Path
    manifest: Mapping[str, Any]
    deployment_id: str
    _temporary: tempfile.TemporaryDirectory[str]
    _content_entries: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        frozen_manifest = _freeze_plain_json(dict(self.manifest))
        frozen_entries = _freeze_plain_json(list(self._content_entries))
        if not isinstance(frozen_manifest, Mapping) or not isinstance(
            frozen_entries, tuple
        ):
            raise TypeError("Prepared deployment state must be JSON-safe.")
        object.__setattr__(self, "manifest", frozen_manifest)
        object.__setattr__(self, "_content_entries", frozen_entries)

    def verify(self) -> None:
        observed = _manifest(self.root)
        observed_entries = _freeze_plain_json(observed["entries"])
        if observed_entries != self._content_entries:
            raise RuntimeError(
                "Prepared deployment bytes no longer match their manifest."
            )
        try:
            published_manifest = json.loads(
                (self.root / "deployment-manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Prepared deployment manifest is unreadable.") from exc
        if published_manifest != thaw_json(self.manifest):
            raise RuntimeError(
                "Prepared deployment metadata does not match its published manifest."
            )
        identity_entries = _freeze_plain_json(self.manifest.get("content_entries"))
        observed_identity_entries = tuple(
            entry
            for entry in observed_entries
            if entry.get("path") != "deployment-manifest.json"
        )
        if identity_entries != observed_identity_entries:
            raise RuntimeError(
                "Prepared deployment identity does not match its content entries."
            )
        identity = {
            key: thaw_json(value)
            for key, value in self.manifest.items()
            if key not in {"deployment_id", "manifest_digest"}
        }
        if (
            self.manifest.get("deployment_id") != self.deployment_id
            or canonical_digest(identity) != self.deployment_id
            or self.manifest.get("manifest_digest") != canonical_digest(identity)
        ):
            raise RuntimeError(
                "Prepared deployment identity no longer matches its manifest."
            )

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> "PreparedDeployment":
        self.verify()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def prepare_deployment(config: RemoteClusterConfig) -> PreparedDeployment:
    """Snapshot every selected local deployment source before network contact."""
    if not config.configured:
        raise ValueError("configuration-incomplete")
    assert config.environment is not None
    assert config.parsl is not None
    assert config.orchestrator is not None
    temporary = tempfile.TemporaryDirectory(prefix="bioimageflow-cluster-deployment-")
    root = Path(temporary.name)
    try:
        uv_runtime: tuple[str, str] | None = None
        if config.environment.kind == "uv":
            uv_runtime = _uv_executable_and_version()
            _validate_uv_frozen(uv_runtime[0], Path(config.environment.source))
        environment_files = _environment_files(config.environment)
        for index, source in enumerate(environment_files):
            _copy_regular(source, root / "environment" / str(index) / source.name)
        environment_plan: dict[str, Any] | None = None
        if config.environment.kind == "uv":
            assert uv_runtime is not None
            captured_pyproject = root / "environment" / "0" / "pyproject.toml"
            captured_lock = root / "environment" / "1" / "uv.lock"
            environment_plan = _prepare_uv_environment(
                config.environment,
                project=Path(config.environment.source),
                captured_pyproject=captured_pyproject,
                captured_lock=captured_lock,
                content_root=root,
                uv_executable=uv_runtime[0],
                uv_version=uv_runtime[1],
                scheduler=config.orchestrator.scheduler,
            )
            _validate_uv_frozen(uv_runtime[0], Path(config.environment.source))
            _verify_snapshot(
                Path(config.environment.source) / "pyproject.toml", captured_pyproject
            )
            _verify_snapshot(Path(config.environment.source) / "uv.lock", captured_lock)
            plan_path = root / "environment" / "install-plan.json"
            plan_path.write_bytes(canonical_json_bytes(environment_plan))
        if config.environment.kind == "wheelhouse":
            captured_lock = root / "environment" / "0" / environment_files[0].name
            captured_wheels = {
                source.name: root / "environment" / str(index) / source.name
                for index, source in enumerate(environment_files[1:], start=1)
            }
            _validate_wheelhouse_files(captured_lock, captured_wheels)
        if config.environment.kind == "uv":
            _capture_project_inputs(
                Path(config.environment.source),
                root / "environment" / "project-inputs" / "0",
            )
        elif config.environment.kind == "pixi":
            project_root = Path(config.environment.source)
            if (project_root / "pyproject.toml").is_file():
                _capture_project_inputs(
                    project_root,
                    root / "environment" / "project-inputs" / "0",
                )
        elif (
            config.environment.kind == "pylock"
            and config.environment.project is not None
        ):
            project = config.environment.project
            project_root = project if project.is_dir() else project.parent
            _capture_project_inputs(
                project_root,
                root / "environment" / "project-inputs" / "0",
            )
        if config.setup is not None and config.setup.source_kind != "cluster_file":
            destination = root / "setup" / "setup.sh"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(config.setup.content)
        if config.parsl.source_kind == "file":
            source_identities: set[tuple[int, int]] = set()
            factory_source = Path(config.parsl.source)
            factory_metadata = factory_source.stat(follow_symlinks=False)
            source_identities.add((factory_metadata.st_dev, factory_metadata.st_ino))
            _copy_regular(
                factory_source,
                root / "parsl" / "factory.py",
                reject_hard_links=True,
            )
            for index, included in enumerate(config.parsl.include):
                included_metadata = included.stat(follow_symlinks=False)
                included_identity = (included_metadata.st_dev, included_metadata.st_ino)
                if included_identity in source_identities:
                    raise ValueError(
                        "Parsl configuration sources must not contain path aliases."
                    )
                source_identities.add(included_identity)
                destination = root / "parsl" / "include" / str(index) / included.name
                if included.is_dir():
                    _copy_tree(included, destination)
                else:
                    _copy_regular(included, destination, reject_hard_links=True)
        content = _manifest(root)
        try:
            version = importlib.metadata.version("bioimageflow")
        except importlib.metadata.PackageNotFoundError:
            version = "uninstalled"
        identity = {
            "schema": DEPLOYMENT_MANIFEST_SCHEMA,
            "environment": _environment_identity(config.environment),
            "environment_ownership": config.environment.ownership,
            "environment_plan": environment_plan,
            "setup": None if config.setup is None else config.setup.to_dict(),
            "parsl": _parsl_identity(config.parsl),
            "scheduler": config.orchestrator.scheduler,
            "bioimageflow_version": version,
            "content_entries": content["entries"],
            "gateway_protocol": 1,
            "factory_runtime_contract": 1,
        }
        deployment_id = canonical_digest(identity)
        manifest: dict[str, Any] = {
            **identity,
            "deployment_id": deployment_id,
            "manifest_digest": canonical_digest(identity),
        }
        (root / "deployment-manifest.json").write_bytes(canonical_json_bytes(manifest))
        complete = _manifest(root)
        # The manifest file contains the pre-publication identity. Its own digest is
        # deliberately outside deployment identity to avoid a recursive digest.
        return PreparedDeployment(
            root,
            manifest,
            deployment_id,
            temporary,
            tuple(complete["entries"]),
        )
    except BaseException:
        temporary.cleanup()
        raise


__all__ = ["DEPLOYMENT_MANIFEST_SCHEMA", "PreparedDeployment", "prepare_deployment"]
