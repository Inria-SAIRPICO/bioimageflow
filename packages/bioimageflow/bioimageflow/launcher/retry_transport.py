"""Strict transport validation for remote retained-run retries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def validate_retry_observation(result: Mapping[str, Any]) -> None:
    """Validate nullable retained retry provenance in one run observation."""
    from .retry import RunRetryPlan
    from .ssh import SSHTransportError

    retry_plan = result.get("retry_plan")
    try:
        parsed = None if retry_plan is None else RunRetryPlan.from_dict(retry_plan)
    except (TypeError, ValueError) as exc:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster observation retry provenance is invalid.",
            ambiguous=True,
        ) from exc
    if parsed is not None and (
        parsed.retry_run_id != result.get("run_id")
        or parsed.storage_path != result.get("storage_path")
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster observation retry provenance changed its run binding.",
            ambiguous=True,
        )


def validate_retry_result(
    operation: str,
    result: dict[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate retry operations, returning ``None`` for other operations."""
    from .retry import RunRetryPlan
    from .ssh import (
        SSHTransportError,
        _OBSERVATION_FIELDS,
        _validate_observation,
    )

    if operation == "plan-retry":
        try:
            plan = RunRetryPlan.from_dict(result)
        except (TypeError, ValueError) as exc:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster retry plan response is invalid.",
                ambiguous=True,
            ) from exc
        if (
            plan.parent_run_id != arguments.get("run_id")
            or plan.storage_path != arguments.get("storage_path")
        ):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster retry preview changed its parent binding.",
                ambiguous=True,
            )
        return result
    if operation != "start-retry":
        return None
    try:
        plan = RunRetryPlan.from_dict(arguments.get("plan"))
    except (TypeError, ValueError) as exc:
        raise SSHTransportError(
            "remote-protocol",
            "Local retry request became invalid.",
            ambiguous=True,
        ) from exc
    expected = {
        "run_id": plan.retry_run_id,
        "storage_path": arguments.get("storage_path"),
    }
    _validate_observation(result, expected)
    if set(result) != _OBSERVATION_FIELDS or result["retry_plan"] != plan.to_dict():
        raise SSHTransportError(
            "remote-protocol",
            "Cluster retry response schema is invalid.",
            ambiguous=True,
        )
    return result
