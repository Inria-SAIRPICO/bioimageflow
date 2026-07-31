from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from bioimageflow import NodeFailureDiagnostic
from bioimageflow.launcher.schemas import (
    ERROR_SCHEMA,
    PROGRESS_SCHEMA,
    STATUS_SCHEMA,
    LauncherSchemaError,
    new_run_id,
    utc_timestamp,
    validate_error,
    validate_progress,
    validate_status,
    validate_submission,
)
from tests.unit.launcher.helpers import (
    backend_progress_payload,
    launcher_submission,
    public_progress_payload,
)


def _status(run_id: str, **updates: Any) -> dict[str, Any]:
    timestamp = utc_timestamp()
    payload = {
        "schema": STATUS_SCHEMA,
        "run_id": run_id,
        "state": "prepared",
        "revision": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
        "backend": "local",
        "orchestrator": None,
        "claim_epoch": None,
        "cancel_requested_at": None,
        "hard_termination_requested": False,
        "error": None,
    }
    payload.update(updates)
    return payload


def _progress(
    run_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": PROGRESS_SCHEMA,
        "run_id": run_id,
        "sequence": 1,
        "timestamp": utc_timestamp(),
        "kind": kind,
        "payload": payload,
    }


def _error(run_id: str, **updates: Any) -> dict[str, Any]:
    payload = {
        "schema": ERROR_SCHEMA,
        "run_id": run_id,
        "code": "workflow-execution-failed",
        "exception_type": "ValueError",
        "message": "failed",
        "traceback": "traceback",
        "node": {"name": "normalize"},
        "task": {
            "task_id": "task-1",
            "invocation_id": "invocation-1",
            "cache_attempt_id": None,
            "row_position": 0,
        },
        "backend": {"name": "local"},
    }
    payload.update(updates)
    return payload


@pytest.mark.parametrize(
    "field, nested_field",
    [
        ("invocation", "unexpected"),
        ("parsl_config", "unexpected"),
        ("task_policy", "unexpected"),
        ("launch", "unexpected"),
        ("protocol_versions", "unexpected"),
    ],
)
def test_submission_rejects_unknown_nested_fields(
    tmp_path: Path,
    field: str,
    nested_field: str,
) -> None:
    payload = launcher_submission(tmp_path, new_run_id())
    nested = payload[field]
    assert isinstance(nested, dict)
    nested[nested_field] = None

    with pytest.raises(LauncherSchemaError, match="unknown fields"):
        validate_submission(payload)


def test_submission_validates_nested_binding_and_workflow_integrity(
    tmp_path: Path,
) -> None:
    payload = launcher_submission(tmp_path, new_run_id())
    binding = payload["executor_bindings"]["threads"]
    binding["capabilities"]["slot"]["unexpected"] = True

    with pytest.raises(LauncherSchemaError, match="invalid"):
        validate_submission(payload)

    payload = launcher_submission(tmp_path, new_run_id())
    payload["workflow"]["payload"]["tampered"] = True
    with pytest.raises(LauncherSchemaError, match="digest does not match"):
        validate_submission(payload)


def test_submission_validates_invocation_records(tmp_path: Path) -> None:
    payload = launcher_submission(tmp_path, new_run_id())
    payload["invocation"]["inputs"] = [
        {
            "id": "settings",
            "kind": "field",
            "name": "settings",
            "value": {"tag": "str", "value": "ok", "unexpected": True},
        }
    ]

    with pytest.raises(LauncherSchemaError, match="typed constant"):
        validate_submission(payload)


@pytest.mark.parametrize(
    "updates, match",
    [
        ({"orchestrator": {"pid": 123}}, "orchestrator"),
        ({"error": "other.json"}, "error"),
        ({"claim_epoch": 1}, "both be set"),
        (
            {
                "state": "running",
                "revision": 2,
            },
            "requires an execution claim",
        ),
        (
            {
                "state": "failed",
                "revision": 1,
            },
            "requires error.json",
        ),
    ],
)
def test_status_rejects_invalid_typed_and_state_dependent_metadata(
    updates: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(LauncherSchemaError, match=match):
        validate_status(_status(new_run_id(), **updates))


def test_status_accepts_claimed_and_preclaim_terminal_shapes() -> None:
    run_id = new_run_id()
    assert (
        validate_status(
            _status(
                run_id,
                state="running",
                revision=2,
                orchestrator="host:123:nonce",
                claim_epoch=1,
            )
        )["state"]
        == "running"
    )
    cancelled_at = utc_timestamp()
    assert (
        validate_status(
            _status(
                run_id,
                state="cancelled",
                revision=1,
                cancel_requested_at=cancelled_at,
            )
        )["state"]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "kind, event",
    [
        ("public", public_progress_payload()),
        ("backend", backend_progress_payload()),
    ],
)
def test_progress_rejects_unknown_nested_fields(
    kind: str,
    event: dict[str, Any],
) -> None:
    event = copy.deepcopy(event)
    event["unexpected"] = True

    with pytest.raises(LauncherSchemaError, match="unknown fields"):
        validate_progress(_progress(new_run_id(), kind=kind, payload=event))


def test_progress_validates_kind_specific_payload_schema() -> None:
    with pytest.raises(LauncherSchemaError, match="payload has"):
        validate_progress(
            _progress(
                new_run_id(),
                kind="public",
                payload=backend_progress_payload(),
            )
        )


def test_progress_accepts_structured_node_diagnostic() -> None:
    diagnostic = NodeFailureDiagnostic(
        scoped_node_path="nested/tool",
        category="execution",
        exception_type="RuntimeError",
        message="failed independently",
        traceback="traceback",
        attempt_id="task-1",
    )

    result = validate_progress(
        _progress(
            new_run_id(),
            kind="diagnostic",
            payload=diagnostic.to_dict(),
        )
    )

    assert result["payload"] == diagnostic.to_dict()


@pytest.mark.parametrize(
    ("event", "native_id", "state"),
    [
        ("psij_queued", "job-1", "ACTIVE"),
        ("psij_unknown", "job-1", "FAILED"),
        ("psij_submission_uncertain", "invented-job", None),
        ("psij_active", " job-1", "ACTIVE"),
    ],
)
def test_progress_rejects_incoherent_psij_observations(
    event: str,
    native_id: str,
    state: str | None,
) -> None:
    payload = {
        "schema": "bioimageflow.launcher.backend_event.v1",
        "event": event,
        "executor": "slurm",
        "native_id": native_id,
        "state": state,
        "message": None,
    }

    with pytest.raises(LauncherSchemaError):
        validate_progress(
            _progress(new_run_id(), kind="backend", payload=payload)
        )


@pytest.mark.parametrize("field", ["node", "task", "backend"])
def test_error_rejects_unknown_nested_fields(field: str) -> None:
    payload = _error(new_run_id())
    nested = payload[field]
    assert isinstance(nested, dict)
    nested["unexpected"] = True

    with pytest.raises(LauncherSchemaError, match="unknown fields"):
        validate_error(payload)


def test_error_validates_nested_field_types() -> None:
    with pytest.raises(LauncherSchemaError, match="row_position"):
        validate_error(
            _error(
                new_run_id(),
                task={
                    "task_id": "task-1",
                    "invocation_id": None,
                    "cache_attempt_id": None,
                    "row_position": True,
                },
            )
        )

    with pytest.raises(LauncherSchemaError, match="returncode"):
        validate_error(
            _error(
                new_run_id(),
                backend={"name": "local", "returncode": True},
            )
        )


def test_error_accepts_backend_exit_metadata() -> None:
    payload = validate_error(
        _error(
            new_run_id(),
            backend={"name": "local", "returncode": -9},
        )
    )

    assert payload["backend"]["returncode"] == -9
