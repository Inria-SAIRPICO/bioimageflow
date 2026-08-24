"""Strict managed-cluster workflow run observation and control."""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping, NoReturn

from bioimageflow.integration import NodeFailureDiagnostic
from bioimageflow.launcher.remote_run import RemoteWorkflowRun as LegacyRemoteWorkflowRun
from bioimageflow.launcher.retry import RecomputeRequest, RunRetryPlan
from bioimageflow.launcher.schemas import (
    REMOTE_RUN_OBSERVATION_SCHEMA,
    parse_utc_timestamp,
    validate_progress,
    validate_run_id,
)

from ._common import DIGEST_RE, exact_dict, freeze_json, normalized_cluster_path, thaw_json
from .reports import ClusterDiagnostic, ClusterOperationError

_RUN_STATES = frozenset(
    {
        "prepared",
        "starting",
        "running",
        "finalizing",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
        "lost",
    }
)
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "lost"})
_OBSERVATION_FIELDS = {
    "schema",
    "error",
    "retry_plan",
    "run_id",
    "state",
    "status_revision",
    "storage_path",
    "terminal",
    "updated_at",
    "attempt_phase",
    "gateway_publication_id",
    "gateway_artifact_digest",
}
_OBSERVATION_ERROR_FIELDS = {"code", "exception_type", "message", "run_id"}
_PROGRESS_PAGE_FIELDS = _OBSERVATION_FIELDS | {
    "events",
    "has_more",
    "next_sequence",
}
_ATTEMPT_PHASES = frozenset(
    {
        "allocated",
        "uploading",
        "ready",
        "scheduler-intent",
        "submitted",
        "rejected",
        "cancelled",
        "uncertain",
    }
)


def _managed_run_id(value: Any) -> str:
    return validate_run_id(value)


def _raise_remote_protocol_error(phase: str, exc: Exception) -> NoReturn:
    raise ClusterOperationError(
        ClusterDiagnostic(
            phase=phase,
            category="protocol-incompatible",
            message="The managed cluster returned malformed run data.",
            retry_safety="safe",
            next_action="inspect-gateway-compatibility",
        )
    ) from exc


@dataclass(frozen=True, slots=True)
class RemoteRunObservation:
    """One exact, independently validated managed run observation."""

    SCHEMA: ClassVar[str] = REMOTE_RUN_OBSERVATION_SCHEMA
    run_id: str
    state: str
    status_revision: int
    storage_path: str
    terminal: bool
    updated_at: str
    attempt_phase: str
    gateway_publication_id: str
    gateway_artifact_digest: str
    error: Mapping[str, Any] | None = None
    retry_plan: RunRetryPlan | None = None

    def __post_init__(self) -> None:
        _managed_run_id(self.run_id)
        if self.state not in _RUN_STATES:
            raise ValueError("Remote observation contains an invalid run state.")
        if type(self.status_revision) is not int or self.status_revision < 0:
            raise ValueError("status_revision must be a non-negative integer.")
        object.__setattr__(
            self,
            "storage_path",
            str(normalized_cluster_path(self.storage_path, field="storage_path")),
        )
        if type(self.terminal) is not bool or self.terminal != (
            self.state in _TERMINAL_STATES
        ):
            raise ValueError("terminal does not match the remote run state.")
        parse_utc_timestamp(self.updated_at, field="updated_at")
        if self.attempt_phase not in _ATTEMPT_PHASES:
            raise ValueError("Remote observation contains an invalid attempt phase.")
        if (
            type(self.gateway_publication_id) is not str
            or not self.gateway_publication_id
        ):
            raise ValueError("Remote observation gateway publication is invalid.")
        if (
            type(self.gateway_artifact_digest) is not str
            or DIGEST_RE.fullmatch(self.gateway_artifact_digest) is None
        ):
            raise ValueError("Remote observation gateway artifact is invalid.")
        if self.error is not None:
            error = exact_dict(
                self.error,
                _OBSERVATION_ERROR_FIELDS,
                "RemoteRunObservation.error",
            )
            if error["run_id"] != self.run_id:
                raise ValueError("Remote observation error changed the run binding.")
            if type(error["code"]) is not str or not error["code"]:
                raise ValueError("Remote observation error code is invalid.")
            for field in ("exception_type", "message"):
                if error[field] is not None and type(error[field]) is not str:
                    raise TypeError(f"Remote observation error {field} is invalid.")
            frozen_error = freeze_json(
                error,
                path="error",
                reject_sensitive_keys=False,
            )
            assert isinstance(frozen_error, Mapping)
            object.__setattr__(self, "error", frozen_error)
        if self.retry_plan is not None and type(self.retry_plan) is not RunRetryPlan:
            raise TypeError("retry_plan must be RunRetryPlan or None.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "error": None if self.error is None else thaw_json(self.error),
            "retry_plan": (
                None if self.retry_plan is None else self.retry_plan.to_dict()
            ),
            "run_id": self.run_id,
            "state": self.state,
            "status_revision": self.status_revision,
            "storage_path": self.storage_path,
            "terminal": self.terminal,
            "updated_at": self.updated_at,
            "attempt_phase": self.attempt_phase,
            "gateway_publication_id": self.gateway_publication_id,
            "gateway_artifact_digest": self.gateway_artifact_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteRunObservation":
        data = exact_dict(value, _OBSERVATION_FIELDS, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported RemoteRunObservation schema.")
        retry_plan = data["retry_plan"]
        return cls(
            run_id=data["run_id"],
            state=data["state"],
            status_revision=data["status_revision"],
            storage_path=data["storage_path"],
            terminal=data["terminal"],
            updated_at=data["updated_at"],
            attempt_phase=data["attempt_phase"],
            gateway_publication_id=data["gateway_publication_id"],
            gateway_artifact_digest=data["gateway_artifact_digest"],
            error=data["error"],
            retry_plan=(
                None if retry_plan is None else RunRetryPlan.from_dict(retry_plan)
            ),
        )


class _ManagedRunType(type):
    def __instancecheck__(cls, instance: Any) -> bool:
        return isinstance(instance, LegacyRemoteWorkflowRun)


class RemoteWorkflowRun(LegacyRemoteWorkflowRun, metaclass=_ManagedRunType):
    """Durable managed run observation and control through the stable gateway."""

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from .client import RemoteCluster

        if args and isinstance(args[0], RemoteCluster):
            return super().__new__(cls)
        instance = LegacyRemoteWorkflowRun.__new__(LegacyRemoteWorkflowRun)
        LegacyRemoteWorkflowRun.__init__(instance, *args, **kwargs)
        return instance

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from .client import RemoteCluster

        if not args or not isinstance(args[0], RemoteCluster):
            LegacyRemoteWorkflowRun.__init__(self, *args, **kwargs)
            return
        if len(args) != 3 or kwargs:
            raise TypeError(
                "Managed RemoteWorkflowRun requires cluster, run_id, and observation."
            )
        cluster, run_id, observation = args
        self._cluster = cluster
        self.id = _managed_run_id(run_id)
        self._observation: RemoteRunObservation
        self._apply_observation(observation)

    @classmethod
    def open(cls, *args: Any) -> Any:
        """Open a managed run, while retaining the old three-argument dispatch."""
        if len(args) == 2:
            from .client import RemoteCluster

            cluster, run_id = args
            if isinstance(cluster, RemoteCluster):
                canonical = _managed_run_id(run_id)
                observation = cluster._request("inspect-run", {"run_id": canonical})
                try:
                    return cls(cluster, canonical, observation)
                except (KeyError, TypeError, ValueError) as exc:
                    _raise_remote_protocol_error("run-observation", exc)
        return LegacyRemoteWorkflowRun.open(*args)

    def _apply_observation(
        self,
        value: Mapping[str, Any] | RemoteRunObservation,
    ) -> None:
        observation = (
            value
            if type(value) is RemoteRunObservation
            else RemoteRunObservation.from_dict(value)
        )
        if observation.run_id != self.id:
            raise ValueError("Remote observation changed the run binding.")
        previous = getattr(self, "_observation", None)
        if previous is not None:
            if observation.status_revision < previous.status_revision:
                raise ValueError("Remote observation revision moved backwards.")
            if (
                observation.status_revision == previous.status_revision
                and observation != previous
            ):
                raise ValueError("Remote observation changed at the same revision.")
        self._status = observation.state
        self._observation = observation

    @property
    def status(self) -> str:
        return self._status

    @property
    def can_cancel(self) -> bool:
        return self.status not in _TERMINAL_STATES

    @property
    def result_available(self) -> bool:
        return self.status == "succeeded"

    def snapshot(self) -> dict[str, Any]:
        return self._observation.to_dict()

    def refresh(self) -> None:
        result = self._cluster._request("refresh-run", {"run_id": self.id})
        try:
            self._apply_observation(result)
        except (KeyError, TypeError, ValueError) as exc:
            _raise_remote_protocol_error("run-observation", exc)

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
    ) -> str:
        if (
            type(poll_interval) not in {int, float}
            or not math.isfinite(float(poll_interval))
            or not 0 < float(poll_interval) <= 3600
        ):
            raise ValueError("poll_interval must be finite and in (0, 3600].")
        if timeout is not None and (
            type(timeout) not in {int, float}
            or not math.isfinite(float(timeout))
            or float(timeout) < 0
        ):
            raise ValueError("timeout must be a finite non-negative number or None.")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            self.refresh()
            if self.status in _TERMINAL_STATES:
                return self.status
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise TimeoutError(f"Workflow run {self.id} did not become terminal.")
            threading.Event().wait(
                float(poll_interval)
                if remaining is None
                else min(float(poll_interval), remaining)
            )

    def progress(self, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer.")
        cursor = after_sequence
        events: list[dict[str, Any]] = []
        while True:
            result = self._cluster._request(
                "read-progress",
                {"run_id": self.id, "after_sequence": cursor, "limit": 500},
            )
            try:
                page = exact_dict(result, _PROGRESS_PAGE_FIELDS, "RemoteProgressPage")
                self._apply_observation(
                    {key: page[key] for key in _OBSERVATION_FIELDS}
                )
                if (
                    type(page["events"]) is not list
                    or type(page["has_more"]) is not bool
                ):
                    raise TypeError("Remote progress page fields have invalid types.")
                if type(page["next_sequence"]) is not int:
                    raise TypeError("Remote progress cursor must be an integer.")
                previous = cursor
                validated: list[dict[str, Any]] = []
                for raw_event in page["events"]:
                    if (
                        not isinstance(raw_event, Mapping)
                        or raw_event.get("run_id") != self.id
                    ):
                        raise ValueError("Remote progress event changed the run binding.")
                    event = validate_progress(raw_event)
                    if event["sequence"] <= previous:
                        raise ValueError(
                            "Remote progress event binding or sequence is invalid."
                        )
                    previous = event["sequence"]
                    validated.append(event)
                if page["next_sequence"] != previous:
                    raise ValueError("Remote progress cursor does not match its events.")
                if page["has_more"] and previous <= cursor:
                    raise ValueError("Remote progress pagination did not advance.")
            except (KeyError, TypeError, ValueError) as exc:
                _raise_remote_protocol_error("run-progress", exc)
            events.extend(validated)
            if not page["has_more"]:
                return events
            cursor = previous

    def diagnostics(self) -> tuple[NodeFailureDiagnostic, ...]:
        try:
            return tuple(
                NodeFailureDiagnostic.from_dict(event["payload"])
                for event in self.progress()
                if event["kind"] == "diagnostic"
            )
        except (KeyError, TypeError, ValueError) as exc:
            _raise_remote_protocol_error("run-diagnostics", exc)

    def cancel(self) -> None:
        if not self.can_cancel:
            self.refresh()
            return
        result = self._cluster._request(
            "cancel-run",
            {"run_id": self.id},
            operation_id=str(uuid.uuid4()),
        )
        try:
            self._apply_observation(result)
        except (KeyError, TypeError, ValueError) as exc:
            _raise_remote_protocol_error("cancellation", exc)

    def plan_retry(self, recompute: RecomputeRequest | None = None) -> RunRetryPlan:
        result = self._cluster._request(
            "plan-retry",
            {
                "run_id": self.id,
                "recompute": None if recompute is None else recompute.to_dict(),
            },
        )
        try:
            return RunRetryPlan.from_dict(
                result["plan"] if "plan" in result else result
            )
        except (KeyError, TypeError, ValueError) as exc:
            _raise_remote_protocol_error("retry-planning", exc)

    def start_retry(self, plan: RunRetryPlan) -> "RemoteWorkflowRun":
        if type(plan) is not RunRetryPlan:
            raise TypeError("plan must be RunRetryPlan.")
        result = self._cluster._request(
            "start-retry",
            {"plan": plan.to_dict()},
            operation_id=plan.retry_run_id,
        )
        try:
            run_id = result.get("run_id", plan.retry_run_id)
            return RemoteWorkflowRun(self._cluster, run_id, result)
        except (KeyError, TypeError, ValueError) as exc:
            _raise_remote_protocol_error("retry-start", exc)

    def download_result(self, destination: str | Path) -> Any:
        return self._cluster._download_result(self.id, Path(destination))

    def export_result(self, destination: str | Path) -> Any:
        return self.download_result(destination)


__all__ = ["RemoteRunObservation", "RemoteWorkflowRun"]
