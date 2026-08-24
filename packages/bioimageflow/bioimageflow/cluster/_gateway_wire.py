"""Standard-library-only validation for gateway-owned wire values."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest

_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")
_PLAN_FIELDS = {
    "schema",
    "host",
    "cluster_root",
    "attempt_id",
    "run_id",
    "deployment_id",
    "external_attestation_digest",
    "invocation_digest",
    "validation_digest",
    "validation_expires_at",
    "validation_evidence",
    "executor_claims",
    "storage_path",
    "expires_at",
    "scheduler_job",
    "task_policy",
    "nodes",
    "plan_digest",
}
_NODE_FIELDS = {
    "node",
    "kind",
    "requirement",
    "compatible_executors",
    "selected_executor",
    "route_reason",
    "cache_status",
    "incompatibilities",
    "will_dispatch",
    "tool_origin",
    "environment_name",
    "environment_identity",
    "storage_mode",
    "diagnostics",
}
_RETRY_FIELDS = {
    "schema",
    "digest",
    "parent_run_id",
    "retry_run_id",
    "parent_status",
    "parent_status_revision",
    "storage_path",
    "retained_submission_digest",
    "retained_material_digest",
    "retained_material_entries",
    "cache_selection_revision",
    "recompute",
    "invalidations",
    "conflicting_run_ids",
}


def _plain_dict(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError("A gateway wire value must be an object.")
    normalized = json.loads(canonical_json_bytes(value))
    if type(normalized) is not dict:
        raise TypeError("A gateway wire value must be an object.")
    return normalized


def _absolute_path(value: Any, *, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be an absolute normalized path.")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value.startswith("//")
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError(f"{field} must be an absolute normalized path.")
    return value


def validate_gateway_run_id(value: Any) -> str:
    if type(value) is not str or _RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("run_id is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class GatewaySchedulerJob:
    value: dict[str, Any]

    @property
    def scheduler(self) -> Any:
        return self.value["scheduler"]

    @property
    def walltime_seconds(self) -> Any:
        return self.value["walltime_seconds"]

    @property
    def queue(self) -> Any:
        return self.value["queue"]

    @property
    def project(self) -> Any:
        return self.value["project"]

    @property
    def cpu(self) -> Any:
        return self.value["cpu"]

    @property
    def gpu(self) -> Any:
        return self.value["gpu"]

    @property
    def memory(self) -> Any:
        return self.value["memory_bytes"]

    @property
    def attributes(self) -> Any:
        return self.value["attributes"]

    @property
    def hard_cancel_after(self) -> Any:
        return self.value["hard_cancel_after_seconds"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json_bytes(self.value))


@dataclass(frozen=True, slots=True)
class GatewayExecutionPlan:
    value: dict[str, Any]
    scheduler_job: GatewaySchedulerJob

    def __getattr__(self, name: str) -> Any:
        if name in _PLAN_FIELDS:
            return self.value[name]
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json_bytes(self.value))


def parse_execution_plan(value: Any) -> GatewayExecutionPlan:
    data = _plain_dict(value)
    if set(data) != _PLAN_FIELDS or data["schema"] != "bioimageflow.remote_execution_plan.v1":
        raise ValueError("Invalid remote execution plan schema.")
    try:
        attempt = uuid.UUID(data["attempt_id"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Invalid execution attempt ID.") from exc
    if attempt.version != 4 or str(attempt) != data["attempt_id"]:
        raise ValueError("Invalid execution attempt ID.")
    validate_gateway_run_id(data["run_id"])
    _absolute_path(data["cluster_root"], field="cluster_root")
    _absolute_path(data["storage_path"], field="storage_path")
    for field in ("deployment_id", "invocation_digest", "validation_digest"):
        if type(data[field]) is not str or DIGEST_RE.fullmatch(data[field]) is None:
            raise ValueError(f"Invalid {field}.")
    external = data["external_attestation_digest"]
    if external is not None and (
        type(external) is not str or DIGEST_RE.fullmatch(external) is None
    ):
        raise ValueError("Invalid external attestation digest.")
    if not all(type(data[field]) is dict for field in ("validation_evidence", "executor_claims", "scheduler_job", "task_policy")):
        raise ValueError("Invalid execution plan mapping.")
    if type(data["nodes"]) is not list or any(
        type(node) is not dict or set(node) != _NODE_FIELDS for node in data["nodes"]
    ):
        raise ValueError("Invalid remote node plan.")
    expected = canonical_digest({key: item for key, item in data.items() if key != "plan_digest"})
    if data["plan_digest"] != expected:
        raise ValueError("Remote execution plan digest mismatch.")
    return GatewayExecutionPlan(data, GatewaySchedulerJob(data["scheduler_job"]))


@dataclass(frozen=True, slots=True)
class GatewayRetryPlan:
    value: dict[str, Any]

    @property
    def parent_run_id(self) -> str:
        return self.value["parent_run_id"]

    @property
    def retry_run_id(self) -> str:
        return self.value["retry_run_id"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json_bytes(self.value))


def parse_retry_plan(value: Any) -> GatewayRetryPlan:
    data = _plain_dict(value)
    if (
        set(data) != _RETRY_FIELDS
        or data["schema"] != "bioimageflow.run_retry_plan.v1"
        or type(data["invalidations"]) is not list
        or type(data["conflicting_run_ids"]) is not list
    ):
        raise ValueError("Invalid retry plan schema.")
    validate_gateway_run_id(data["parent_run_id"])
    validate_gateway_run_id(data["retry_run_id"])
    if data["parent_run_id"] == data["retry_run_id"]:
        raise ValueError("Retry run ID must differ from its parent.")
    body = {key: item for key, item in data.items() if key != "digest"}
    if data["digest"] != canonical_digest(body):
        raise ValueError("Retry plan digest mismatch.")
    return GatewayRetryPlan(data)


__all__ = [
    "GatewayExecutionPlan",
    "GatewayRetryPlan",
    "parse_execution_plan",
    "parse_retry_plan",
    "validate_gateway_run_id",
]
