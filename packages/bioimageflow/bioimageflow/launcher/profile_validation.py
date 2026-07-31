"""Non-submitting cluster-side and public remote-profile validation."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping

from bioimageflow.parsl import ExecutorBinding

from .types import (
    PSIJLaunchConfig,
    ParslConfigRef,
    SSHSubmissionTransport,
    _absolute_posix_path,
)


def _sanitized_profile_error(
    exception: BaseException,
    config_ref: ParslConfigRef | None,
) -> str:
    message = str(exception) or type(exception).__name__
    if config_ref is None or config_ref.secret_refs is None:
        return message
    for reference in config_ref.secret_refs.values():
        secret = os.environ.get(reference)
        if secret:
            message = message.replace(secret, "<redacted>")
    return message


@dataclass(frozen=True, slots=True)
class RemoteProfileDiagnostic:
    """Stable sanitized diagnostic produced on the remote execution host."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteProfileDiagnostic":
        if not isinstance(value, dict) or set(value) != {"code", "message"}:
            raise ValueError("Invalid RemoteProfileDiagnostic payload.")
        if type(value["code"]) is not str or type(value["message"]) is not str:
            raise TypeError("RemoteProfileDiagnostic fields must be strings.")
        return cls(code=value["code"], message=value["message"])


@dataclass(frozen=True, slots=True)
class RemoteProfileValidationReport:
    SCHEMA: ClassVar[str] = "bioimageflow.remote_profile_validation.v1"
    valid: bool
    diagnostics: tuple[RemoteProfileDiagnostic, ...]
    executor_labels: tuple[str, ...] = ()
    allocation_created: bool = False
    workflow_run_created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "valid": self.valid,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "executor_labels": list(self.executor_labels),
            "allocation_created": self.allocation_created,
            "workflow_run_created": self.workflow_run_created,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteProfileValidationReport":
        if not isinstance(value, dict) or value.get("schema") != cls.SCHEMA:
            raise ValueError("Invalid RemoteProfileValidationReport payload.")
        return cls(
            valid=value["valid"],
            diagnostics=tuple(
                RemoteProfileDiagnostic.from_dict(item)
                for item in value["diagnostics"]
            ),
            executor_labels=tuple(value["executor_labels"]),
            allocation_created=value["allocation_created"],
            workflow_run_created=value["workflow_run_created"],
        )


def validate_profile_on_cluster(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate imports, secrets, labels, paths, and PSI/J without submission."""
    diagnostics: list[RemoteProfileDiagnostic] = []
    labels: tuple[str, ...] = ()
    config_ref: ParslConfigRef | None = None
    try:
        config_ref = ParslConfigRef.from_dict(arguments["parsl_config"])
        bindings_payload = arguments["executor_bindings"]
        if not isinstance(bindings_payload, dict):
            raise TypeError("executor_bindings must be an object.")
        bindings = {
            label: ExecutorBinding.from_dict(value)
            for label, value in bindings_payload.items()
        }
        launch = PSIJLaunchConfig.from_dict(arguments["launch"])
        _absolute_posix_path(arguments["storage_path"], field="storage_path")
        _absolute_posix_path(arguments["staging_root"], field="staging_root")
        from .configuration import build_parsl_config

        config = build_parsl_config(
            config_ref,
            trusted_factories=(config_ref.factory,),
        )
        retries = getattr(config, "retries", None)
        if retries != 0:
            diagnostics.append(
                RemoteProfileDiagnostic(
                    "parsl-retries",
                    "Parsl Config.retries must be exactly 0.",
                )
            )
        executors = getattr(config, "executors", ())
        labels = tuple(
            sorted(
                label
                for executor in executors
                if isinstance((label := getattr(executor, "label", None)), str)
            )
        )
        missing = sorted(set(bindings).difference(labels))
        if missing:
            diagnostics.append(
                RemoteProfileDiagnostic(
                    "executor-labels",
                    f"Config is missing declared executor labels: {missing}.",
                )
            )
        if launch.work_dir is not None:
            work_dir = Path(launch.work_dir)
            if not work_dir.is_dir():
                diagnostics.append(
                    RemoteProfileDiagnostic(
                        "work-directory",
                        "PSI/J work_dir is not an existing directory.",
                    )
                )
        from .psij import _load_runtime

        runtime = _load_runtime()
        available = runtime.JobExecutor.get_executor_names()
        if launch.executor not in available:
            diagnostics.append(
                RemoteProfileDiagnostic(
                    "psij-executor",
                    f"PSI/J executor {launch.executor!r} is unavailable.",
                )
            )
    except Exception as exc:
        details = getattr(exc, "details", {})
        secret_ref = details.get("secret_ref") if isinstance(details, dict) else None
        diagnostics.append(
            RemoteProfileDiagnostic(
                "missing-secret-reference" if secret_ref else "profile-validation",
                (
                    f"Required secret reference {secret_ref!r} is unavailable."
                    if secret_ref
                    else _sanitized_profile_error(exc, config_ref)
                ),
            )
        )
    return RemoteProfileValidationReport(
        valid=not diagnostics,
        diagnostics=tuple(diagnostics),
        executor_labels=labels,
    ).to_dict()


def validate_remote_execution_profile(
    *,
    transport: SSHSubmissionTransport,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    launch: PSIJLaunchConfig,
    storage_path: str,
) -> RemoteProfileValidationReport:
    """Test a remote submitted profile without a workflow run or scheduler job."""
    from .ssh import execute_cluster_command

    result = execute_cluster_command(
        transport,
        "validate-profile",
        {
            "parsl_config": parsl_config.to_dict(),
            "executor_bindings": {
                label: binding.to_dict()
                for label, binding in executor_bindings.items()
            },
            "launch": launch.to_dict(),
            "storage_path": storage_path,
            "staging_root": str(transport.staging_root),
        },
        request_id=str(uuid.uuid4()),
    )
    return RemoteProfileValidationReport.from_dict(result)


__all__ = [
    "RemoteProfileDiagnostic",
    "RemoteProfileValidationReport",
    "validate_remote_execution_profile",
]
