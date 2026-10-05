"""Exact local result-group handles, strongly pinned only by returned references."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
import threading
from typing import Any, TYPE_CHECKING
import weakref

import numpy as np
import pandas as pd

from bioimageflow_core import CleanupStatus, ConsumedRow, SharedArray, SharedMemoryContext

if TYPE_CHECKING:
    from bioimageflow_core import SharedArrayLease


def _release_leases(leases: tuple[SharedArrayLease, ...]) -> None:
    for lease in leases:
        lease.release()


@dataclass(frozen=True)
class ResultGroup:
    """Captured group identity and exact allocation leases; never reference objects."""
    node_name: str
    group_id: str
    consumed_rows: tuple[ConsumedRow, ...]
    _leases: tuple[SharedArrayLease, ...] = field(repr=False, compare=False)
    _resources: tuple[tuple[str, str, str], ...] = field(repr=False, compare=False)
    _finalizer: Any = field(init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.node_name or not self.group_id:
            raise ValueError("Result groups require captured provider and group identities")
        object.__setattr__(self, "_finalizer", weakref.finalize(self, _release_leases, self._leases))

    @property
    def released(self) -> bool:
        """Whether this exact group's release has been requested."""
        return not self._finalizer.alive

    def release(self) -> CleanupStatus:
        """Release only this group; existing readers/grants may keep backing pending."""
        with self._lock:
            self._finalizer()
            return self.status()

    def status(self) -> CleanupStatus:
        """Observe exact lease cleanup rather than a workflow/manager outcome."""
        states = [lease.status() for lease in self._leases]
        allocations: dict[tuple[str, str, str], CleanupStatus] = {}
        grants: dict[str, int] = {}
        for resource, state in zip(self._resources, states, strict=True):
            allocations[resource] = state
            owner_id, _, _ = resource
            grants[owner_id] = max(grants.get(owner_id, 0), state.pending_grants)
        return CleanupStatus(
            "closed" if all(state.state == "closed" for state in states) else "pending",
            sum(state.pending_readers for state in allocations.values()),
            sum(grants.values()),
            sum(state.pending_files for state in allocations.values()),
            tuple(dict.fromkeys(error for state in states for error in state.errors)),
            sum(state.pending_leases for state in allocations.values()),
        )

    def _owns_reference(self, ref: SharedArray) -> bool:
        return any(lease.owns(ref) for lease in self._leases)


def _references(value: Any):
    if isinstance(value, SharedArray):
        yield value
    elif isinstance(value, pd.DataFrame):
        for column in value.columns:
            for cell in value[column].array:
                yield from _references(cell)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _references(child)


def result_groups(value: Any) -> tuple[ResultGroup, ...]:
    """Discover captured handles from actual returned refs, without I/O or attrs."""
    groups: dict[str, ResultGroup] = {}
    for ref in _references(value):
        group = ref.bound_group
        if not isinstance(group, ResultGroup) or not group._owns_reference(ref):
            raise ValueError("Shared reference has no accepted result group or has a foreign binding")
        previous = groups.setdefault(group.group_id, group)
        if previous is not group:
            raise ValueError("Conflicting result group identity")
    return tuple(groups.values())


def _project_value(value: Any, group: ResultGroup, leases: dict[tuple[Any, ...], SharedArrayLease]) -> Any:
    if isinstance(value, SharedArray):
        return leases[_key(value)].project(group)
    if isinstance(value, pd.DataFrame):
        return map_shared_values(value, lambda cell: _project_value(cell, group, leases))
    if isinstance(value, dict):
        return {name: _project_value(child, group, leases) for name, child in value.items()}
    if isinstance(value, list):
        return [_project_value(child, group, leases) for child in value]
    if isinstance(value, tuple):
        return tuple(_project_value(child, group, leases) for child in value)
    return value


def _contains_array(value: Any) -> bool:
    if isinstance(value, (SharedArray, np.ndarray)):
        return True
    if isinstance(value, dict):
        return any(_contains_array(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_array(child) for child in value)
    return False


def working_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Detach execution-owned blocks and mutable native pixels before a merge hook."""
    def copy_pixels(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return np.array(value, copy=True, order="K")
        if isinstance(value, dict):
            return {name: copy_pixels(child) for name, child in value.items()}
        if isinstance(value, list):
            return [copy_pixels(child) for child in value]
        if isinstance(value, tuple):
            return tuple(copy_pixels(child) for child in value)
        return value
    owned = pd.DataFrame(frame, copy=True)
    owned.index = frame.index.copy(deep=True)
    owned.columns = frame.columns.copy(deep=True)
    for column in frame.columns:
        if frame[column].dtype == object:
            owned[column] = pd.Series([copy_pixels(value) for value in frame[column].array],
                                      index=owned.index, dtype=object)
    return owned


def map_shared_values(frame: pd.DataFrame, transform: Callable[[Any], Any]) -> pd.DataFrame:
    """Admit array-bearing cells together without copying attrs or ordinary columns."""
    result = pd.DataFrame(frame, copy=False)
    shared = {}
    for column in frame.columns:
        cells = frame[column].array
        if any(_contains_array(cell) for cell in cells):
            shared[column] = list(cells)
    if shared:
        admitted = transform(shared)
        for column in shared:
            result[column] = pd.Series(admitted[column], index=frame.index, dtype=object)
    return result


def _key(ref: SharedArray) -> tuple[Any, ...]:
    return (id(ref.bound_owner), ref.scope_id, ref.name, ref.shape, ref.dtype)


def bind_result_group(value: Any, *, node_name: str, group_id: str,
                      consumed_rows: tuple[ConsumedRow, ...] = ()) -> tuple[Any, ResultGroup | None]:
    """Pin already sealed values to one group; this helper performs no pixel copy."""
    refs: dict[tuple[Any, ...], SharedArray] = {}
    for ref in _references(value):
        if not isinstance(ref.bound_owner, SharedMemoryContext):
            raise ValueError("Result groups require accepted controller-bound references")
        refs.setdefault(_key(ref), ref)
    if not refs:
        return value, None
    leases = {}
    try:
        for key, ref in refs.items():
            leases[key] = ref.bound_owner.retain(ref)
        resources = tuple((ref.bound_owner.descriptor()["owner_id"], ref.scope_id, ref.name)
                          for ref in refs.values())
        group = ResultGroup(node_name, group_id, tuple(consumed_rows), tuple(leases.values()), resources)
        return _project_value(value, group, leases), group
    except BaseException:
        for lease in leases.values():
            lease.cancel_retention()
        raise
