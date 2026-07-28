"""Pure revisioned state-machine operations for submitted workflow runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import LauncherStateConflictError
from .schemas import (
    TERMINAL_STATES,
    parse_utc_timestamp,
    utc_timestamp,
    validate_status,
)


LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"starting", "cancelled", "failed"}),
    "starting": frozenset({"running", "cancel_requested", "failed", "lost"}),
    "running": frozenset({"cancel_requested", "finalizing", "failed", "lost"}),
    "finalizing": frozenset({"succeeded", "failed", "lost"}),
    "cancel_requested": frozenset({"cancelled", "failed", "lost"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "lost": frozenset(),
}

_MUTABLE_STATUS_FIELDS = frozenset(
    {
        "orchestrator",
        "claim_epoch",
        "cancel_requested_at",
        "hard_termination_requested",
        "error",
    }
)


class LauncherStateError(LauncherStateConflictError):
    """Base class for guarded launcher state failures."""


class RevisionConflictError(LauncherStateError):
    """Raised when status changed after a caller's observed revision."""


class ClaimEpochMismatchError(LauncherStateError):
    """Raised when a claimed-run mutation names the wrong claim epoch."""


class InvalidTransitionError(LauncherStateError):
    """Raised for a state transition outside the exact launcher graph."""


def is_terminal(state: str) -> bool:
    """Return whether a launcher state is immutable."""
    return state in TERMINAL_STATES


def transition_status(
    status: Mapping[str, Any],
    *,
    expected_revision: int,
    new_state: str,
    expected_claim_epoch: int | None = None,
    updated_at: str | None = None,
    updates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the next validated status after one exact CAS transition."""
    current = validate_status(status)
    if current["revision"] != expected_revision:
        raise RevisionConflictError(
            f"Expected status revision {expected_revision}, "
            f"found {current['revision']}."
        )
    current_epoch = current["claim_epoch"]
    if current_epoch is not None and expected_claim_epoch != current_epoch:
        raise ClaimEpochMismatchError(
            f"Expected claim epoch {expected_claim_epoch!r}, "
            f"found {current_epoch!r}."
        )
    if current_epoch is None and expected_claim_epoch is not None:
        raise ClaimEpochMismatchError(
            f"Run has no status claim epoch, got {expected_claim_epoch!r}."
        )
    if new_state not in LEGAL_TRANSITIONS[current["state"]]:
        raise InvalidTransitionError(
            f"Illegal launcher transition {current['state']!r} -> {new_state!r}."
        )
    changes = dict(updates or {})
    unknown = frozenset(changes) - _MUTABLE_STATUS_FIELDS
    if unknown:
        raise InvalidTransitionError(
            f"Status transition cannot change fields {sorted(unknown)}."
        )
    if (
        new_state == "starting"
        and current["state"] == "prepared"
        and changes.get("claim_epoch") is None
    ):
        raise InvalidTransitionError(
            "prepared -> starting requires the acquired claim epoch."
        )
    if (
        current["state"] != "prepared"
        and "claim_epoch" in changes
        and changes["claim_epoch"] != current["claim_epoch"]
    ):
        raise InvalidTransitionError(
            "Claim epochs change only through guarded claim takeover."
        )
    next_status = dict(current)
    next_status.update(changes)
    next_status["state"] = new_state
    next_status["revision"] = expected_revision + 1
    next_status["updated_at"] = updated_at or utc_timestamp()
    if parse_utc_timestamp(
        next_status["updated_at"],
        field="updated_at",
    ) < parse_utc_timestamp(current["updated_at"], field="updated_at"):
        raise InvalidTransitionError("updated_at must be monotonic.")
    return validate_status(next_status)


def revise_status_metadata(
    status: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_claim_epoch: int,
    updated_at: str | None = None,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Revise claimed non-terminal metadata without changing its state."""
    current = validate_status(status)
    if current["state"] in TERMINAL_STATES:
        raise InvalidTransitionError(
            f"Terminal launcher state {current['state']!r} is immutable."
        )
    if current["revision"] != expected_revision:
        raise RevisionConflictError(
            f"Expected status revision {expected_revision}, "
            f"found {current['revision']}."
        )
    if current["claim_epoch"] != expected_claim_epoch:
        raise ClaimEpochMismatchError(
            f"Expected claim epoch {expected_claim_epoch!r}, "
            f"found {current['claim_epoch']!r}."
        )
    changes = dict(updates)
    unknown = frozenset(changes) - _MUTABLE_STATUS_FIELDS
    if unknown:
        raise InvalidTransitionError(
            f"Status revision cannot change fields {sorted(unknown)}."
        )
    next_status = dict(current)
    next_status.update(changes)
    next_status["revision"] = expected_revision + 1
    next_status["updated_at"] = updated_at or utc_timestamp()
    if parse_utc_timestamp(
        next_status["updated_at"],
        field="updated_at",
    ) < parse_utc_timestamp(current["updated_at"], field="updated_at"):
        raise InvalidTransitionError("updated_at must be monotonic.")
    return validate_status(next_status)
