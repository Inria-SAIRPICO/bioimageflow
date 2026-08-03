"""Immutable public LocalUpload preparation boundary."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.workflow import Workflow

from .cluster_bundle import (
    PreparedClusterBundle,
    _manifest,
    prepare_cluster_bundle,
)
from .remote_run import RemoteWorkflowRun
from .pre_launch import PreLaunchScript
from .types import (
    PSIJLaunchConfig,
    ParslConfigRef,
    SSHSubmissionTransport,
)


@dataclass(frozen=True, slots=True)
class PreparedSubmissionEntry:
    """One immutable file or directory entry in a prepared bundle."""

    path: str
    kind: Literal["file", "directory"]
    size: int
    digest: str

    def __post_init__(self) -> None:
        pure = PurePosixPath(self.path)
        if (
            type(self.path) is not str
            or not self.path
            or pure.is_absolute()
            or str(pure) != self.path
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError("Prepared submission entry path is invalid.")
        if self.kind not in {"file", "directory"}:
            raise ValueError("Prepared submission entry kind is invalid.")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("Prepared submission entry size is invalid.")
        if self.kind == "directory" and self.size != 0:
            raise ValueError("Prepared submission directories must have size zero.")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest) is None:
            raise ValueError("Prepared submission entry digest is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "kind": self.kind,
            "path": self.path,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedSubmissionEntry":
        if not isinstance(value, dict) or set(value) != {
            "digest",
            "kind",
            "path",
            "size",
        }:
            raise ValueError("Invalid PreparedSubmissionEntry payload.")
        return cls(
            path=value["path"],
            kind=value["kind"],
            size=value["size"],
            digest=value["digest"],
        )


@dataclass(frozen=True, slots=True)
class PreparedSubmissionExternalSource:
    """One cluster-side source whose bytes are observed during submission."""

    kind: Literal["cluster_pre_launch"]
    path: str
    expected_digest: str | None = None

    def __post_init__(self) -> None:
        pure = PurePosixPath(self.path)
        if (
            self.kind != "cluster_pre_launch"
            or type(self.path) is not str
            or any(character in self.path for character in ("\x00", "\n", "\r"))
            or not pure.is_absolute()
            or self.path.startswith("//")
            or str(pure) != self.path
            or any(part in {"", ".", ".."} for part in pure.parts[1:])
        ):
            raise ValueError("Prepared external source is invalid.")
        if (
            self.expected_digest is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.expected_digest) is None
        ):
            raise ValueError("Prepared external source digest is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "expected_digest": self.expected_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedSubmissionExternalSource":
        if not isinstance(value, dict) or set(value) != {
            "kind",
            "path",
            "expected_digest",
        }:
            raise ValueError("Invalid PreparedSubmissionExternalSource payload.")
        return cls(
            kind=value["kind"],
            path=value["path"],
            expected_digest=value["expected_digest"],
        )


@dataclass(frozen=True, slots=True)
class PreparedSubmissionManifest:
    """Serializable digest manifest for a prepared remote invocation."""

    SCHEMA: ClassVar[str] = "bioimageflow.prepared_submission_manifest.v2"
    bundle_digest: str
    entries: tuple[PreparedSubmissionEntry, ...]
    external_sources: tuple[PreparedSubmissionExternalSource, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.bundle_digest) is None:
            raise ValueError("Prepared submission bundle digest is invalid.")
        if type(self.entries) is not tuple or any(
            type(entry) is not PreparedSubmissionEntry for entry in self.entries
        ):
            raise TypeError("entries must contain PreparedSubmissionEntry values.")
        if type(self.external_sources) is not tuple or any(
            type(source) is not PreparedSubmissionExternalSource
            for source in self.external_sources
        ):
            raise TypeError(
                "external_sources must contain PreparedSubmissionExternalSource values."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "bundle_digest": self.bundle_digest,
            "entries": [entry.to_dict() for entry in self.entries],
            "external_sources": [source.to_dict() for source in self.external_sources],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreparedSubmissionManifest":
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "bundle_digest", "entries", "external_sources"}
            or value["schema"] != cls.SCHEMA
            or not isinstance(value["entries"], list)
            or not isinstance(value["external_sources"], list)
        ):
            raise ValueError("Invalid PreparedSubmissionManifest payload.")
        return cls(
            bundle_digest=value["bundle_digest"],
            entries=tuple(
                PreparedSubmissionEntry.from_dict(entry) for entry in value["entries"]
            ),
            external_sources=tuple(
                PreparedSubmissionExternalSource.from_dict(source)
                for source in value["external_sources"]
            ),
        )


class PreparedRemoteSubmission:
    """Owned immutable staged bytes that can be submitted exactly once."""

    def __init__(
        self,
        *,
        context: Any,
        bundle: PreparedClusterBundle,
        storage_path: str,
        expires_at: float,
    ) -> None:
        self._context = context
        self._bundle = bundle
        self._storage_path = storage_path
        self._expires_at = expires_at
        self._closed = False
        self._submitted = False
        self.manifest = PreparedSubmissionManifest(
            bundle_digest=bundle.digest,
            entries=tuple(
                PreparedSubmissionEntry.from_dict(entry)
                for entry in bundle.manifest["entries"]
            ),
            external_sources=tuple(
                PreparedSubmissionExternalSource.from_dict(source)
                for source in bundle.external_sources
            ),
        )

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(
        self,
        transport: SSHSubmissionTransport,
    ) -> RemoteWorkflowRun:
        """Submit only the staged bytes, never the original LocalUpload paths."""
        if self._closed:
            raise RuntimeError("Prepared submission is closed.")
        if self._submitted:
            raise RuntimeError("Prepared submission was already submitted.")
        if self.expired:
            self.close()
            raise RuntimeError("Prepared submission expired.")
        if _manifest(self._bundle.root) != self._bundle.manifest:
            raise RuntimeError(
                "Prepared submission bytes no longer match the manifest."
            )
        from .prepared_transport import submit_prepared_cluster_bundle

        run_id = submit_prepared_cluster_bundle(
            self._bundle,
            transport=transport,
            storage_path=self._storage_path,
        )
        self._submitted = True
        run = RemoteWorkflowRun._submitted(
            transport,
            self._storage_path,
            run_id,
        )
        self.close()
        return run

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context.__exit__(None, None, None)

    def __enter__(self) -> "PreparedRemoteSubmission":
        if self._closed:
            raise RuntimeError("Prepared submission is closed.")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def prepare_remote_submission(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None,
    targets: Sequence[str] | None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    launch: PSIJLaunchConfig,
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: Path | str | None = None,
    task_policy: ParslTaskPolicy | None = None,
    pre_launch: PreLaunchScript | None = None,
    lifetime: float = 3600.0,
) -> PreparedRemoteSubmission:
    """Prepare immutable remote invocation bytes without network operations."""
    if type(lifetime) not in {int, float} or lifetime <= 0:
        raise ValueError("lifetime must be a positive number of seconds.")
    context = prepare_cluster_bundle(
        workflow,
        inputs=inputs,
        targets=targets,
        parsl_config=parsl_config,
        executor_bindings=executor_bindings,
        node_routes=node_routes,
        environment_routes=environment_routes,
        shared_runtime_root=shared_runtime_root,
        task_policy=task_policy,
        launch=launch,
        pre_launch=pre_launch,
    )
    try:
        bundle = context.__enter__()
    except BaseException:
        context.__exit__(None, None, None)
        raise
    return PreparedRemoteSubmission(
        context=context,
        bundle=bundle,
        storage_path=workflow.storage_path.as_posix(),
        expires_at=time.monotonic() + float(lifetime),
    )


__all__ = [
    "PreparedRemoteSubmission",
    "PreparedSubmissionEntry",
    "PreparedSubmissionExternalSource",
    "PreparedSubmissionManifest",
    "prepare_remote_submission",
]
