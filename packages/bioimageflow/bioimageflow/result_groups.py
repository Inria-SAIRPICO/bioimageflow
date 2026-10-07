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
    from bioimageflow_core import SharedArrayLease, WorkerGrant


def _release_leases(leases: tuple[SharedArrayLease, ...]) -> None:
    for lease in leases:
        lease.release()


def _cleanup_error(error: BaseException) -> str:
    try:
        detail = str(error)
    except BaseException:
        detail = "<unprintable exception>"
    return f"{type(error).__name__}: {detail}"


class _ReturnRoot:
    """Local disposal authority for one root created by public-return hydration."""

    def __init__(self, owner: SharedMemoryContext) -> None:
        self.owner = owner
        self.owner_id = owner.descriptor()["owner_id"]
        self.lock = threading.RLock()
        self.admissions: set[str] = set()
        self.groups: dict[int, _OwnedGroupDisposal] = {}
        self.requested = False
        self.closed = False

    def register(self, group: _OwnedGroupDisposal) -> set[str]:
        with self.lock:
            scopes = {scope for owner, scope, _name in group.resources if owner == self.owner_id}
            admitted = self.admissions & scopes
            self.groups[id(group)] = group
            self.admissions.difference_update(scopes)
            return admitted

    def cancel(self, group: _OwnedGroupDisposal, admissions: set[str]) -> None:
        with self.lock:
            self.groups.pop(id(group), None)
            self.admissions.update(admissions)

    def retry_disposals(self) -> None:
        with self.lock:
            pending = tuple(group for group in self.groups.values() if group.requested)
        # Do not invert root/group lock ordering against a concurrent finalizer.
        primary: BaseException | None = None
        for group in pending:
            try:
                group.release()
            except BaseException as error:
                if primary is None:
                    primary = error
        if primary is not None:
            raise primary

    def retry(self) -> CleanupStatus:
        with self.lock:
            if self.closed:
                return CleanupStatus("closed", 0, 0, 0, (), 0)
            status = SharedMemoryContext.status(self.owner)
            if not self.requested:
                return status
            # Released groups may still have their own live mapped views. Foreign
            # allocations and retentions must remain open until their owners settle.
            files: dict[tuple[str, str, str], int] = {}
            for group in tuple(self.groups.values()):
                retained = False
                for resource, lease in zip(group.resources, group.leases, strict=True):
                    if resource[0] == self.owner_id:
                        allocation = lease.status()
                        files[resource] = allocation.pending_files
                        retained = retained or allocation.state != "closed"
                if group.requested and not group.errors and not retained:
                    self.groups.pop(id(group), None)
            active = self.admissions or any(not group.requested for group in self.groups.values())
            if active:
                return CleanupStatus("pending", status.pending_readers, status.pending_grants,
                                     status.pending_files, status.errors, status.pending_leases)
            if (status.pending_leases or status.pending_grants
                    or status.pending_files > sum(files.values())):
                return status
            status = SharedMemoryContext.close(self.owner)
            if status.state == "closed":
                self.closed = True
                self.groups.clear()
            return status


class _ReturnMemoryContext(SharedMemoryContext):
    """SDK-owned scopes propagate captured root disposal through public methods."""

    _return_root: _ReturnRoot

    def __init__(self, root: Any) -> None:
        super().__init__(root)
        self._return_root = _ReturnRoot(self)

    @classmethod
    def borrow(cls, descriptor: Any, inputs: Any = ()) -> SharedMemoryContext:
        # Borrowed worker scopes receive no controller deletion authority.
        return SharedMemoryContext.borrow(descriptor, inputs)

    def task_scope(self, task_id: str) -> SharedMemoryContext:
        holder = self._return_root
        with holder.lock:
            scope = super().task_scope(task_id)
            assert isinstance(scope, _ReturnMemoryContext)
            scope._return_root = holder
            holder.admissions.add(scope.scope_id)
            return scope

    def create(self, data: Any, name: str | None = None) -> SharedArray:
        with self._return_root.lock:
            return super().create(data, name=name)

    def retain(self, ref: SharedArray) -> SharedArrayLease:
        with self._return_root.lock:
            return super().retain(ref)

    def acquire_worker_grant(self, inputs: Any = ()) -> WorkerGrant:
        with self._return_root.lock:
            return super().acquire_worker_grant(inputs)

    def accept_result(self, value: Any) -> Any:
        with self._return_root.lock:
            return super().accept_result(value)

    def publish(self, ref: SharedArray) -> SharedArray:
        with self._return_root.lock:
            return super().publish(ref)

    def publish_value(self, value: Any) -> Any:
        with self._return_root.lock:
            return super().publish_value(value)

    def discard_unreturned(self) -> CleanupStatus:
        holder = self._return_root
        with holder.lock:
            status = super().discard_unreturned()
            holder.admissions.discard(self.scope_id)
            return holder.retry() if holder.requested else status

    def status(self) -> CleanupStatus:
        holder = self._return_root
        holder.retry_disposals()
        with holder.lock:
            status = super().status()
            return holder.retry() if holder.requested else status

    def close(self) -> CleanupStatus:
        holder = self._return_root
        with holder.lock:
            if self.scope_id == holder.owner_id:
                holder.requested = True
        holder.retry_disposals()
        with holder.lock:
            status = super().close()
            holder.admissions.discard(self.scope_id)
            return holder.retry() if holder.requested else status


class _OwnedGroupDisposal:
    """Finalizer payload: exact leases and owners, never result groups or refs."""

    def __init__(self, leases: tuple[SharedArrayLease, ...],
                 resources: tuple[tuple[str, str, str], ...], roots: tuple[_ReturnRoot, ...]) -> None:
        self.leases, self.resources, self.roots = leases, resources, roots
        self.requested = False
        self.admissions: dict[_ReturnRoot, set[str]] = {}
        self.errors: dict[tuple[str, int], str] = {}
        self.lock = threading.RLock()

    def commit(self) -> None:
        for root in self.roots:
            self.admissions[root] = root.register(self)

    def cancel(self) -> None:
        for root, admissions in self.admissions.items():
            root.cancel(self, admissions)
        self.admissions.clear()

    def release(self) -> None:
        with self.lock:
            self.requested = True
            primary: BaseException | None = None
            for lease in self.leases:
                key = ("lease", id(lease))
                try:
                    lease.release()
                    self.errors.pop(key, None)
                except BaseException as error:
                    self.errors[key] = _cleanup_error(error)
                    if primary is None:
                        primary = error
            for root in self.admissions:
                key = ("root", id(root))
                try:
                    with root.lock:
                        root.requested = True
                        root.retry()
                    self.errors.pop(key, None)
                except BaseException as error:
                    self.errors[key] = _cleanup_error(error)
                    if primary is None:
                        primary = error
            if primary is not None:
                raise primary


@dataclass(frozen=True)
class ResultGroup:
    """Captured group identity and exact allocation leases; never reference objects."""
    node_name: str
    group_id: str
    consumed_rows: tuple[ConsumedRow, ...]
    _leases: tuple[SharedArrayLease, ...] = field(repr=False, compare=False)
    _resources: tuple[tuple[str, str, str], ...] = field(repr=False, compare=False)
    _return_roots: tuple[_ReturnRoot, ...] = field(default=(), repr=False, compare=False)
    _disposal: _OwnedGroupDisposal | None = field(default=None, init=False, repr=False, compare=False)
    _finalizer: Any = field(init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.node_name or not self.group_id:
            raise ValueError("Result groups require captured provider and group identities")
        if self._return_roots:
            disposal = _OwnedGroupDisposal(self._leases, self._resources, self._return_roots)
            object.__setattr__(self, "_disposal", disposal)
            finalizer = weakref.finalize(self, disposal.release)
        else:
            finalizer = weakref.finalize(self, _release_leases, self._leases)
        object.__setattr__(self, "_finalizer", finalizer)

    @property
    def released(self) -> bool:
        """Whether this exact group's release has been requested."""
        return not self._finalizer.alive

    def release(self) -> CleanupStatus:
        """Release only this group; existing readers/grants may keep backing pending."""
        with self._lock:
            if self._finalizer.alive:
                self._finalizer()
            elif self._disposal is not None:
                self._disposal.release()
            return self.status()

    def status(self) -> CleanupStatus:
        """Observe exact lease cleanup rather than a workflow/manager outcome."""
        if self._disposal is not None and self._disposal.requested:
            self._disposal.release()
        roots = {root.owner_id: root.retry() for root in self._return_roots}
        states = [lease.status() for lease in self._leases]
        allocations: dict[tuple[str, str, str], CleanupStatus] = {}
        grants: dict[str, int] = {}
        for resource, state in zip(self._resources, states, strict=True):
            if resource[0] in roots:
                continue
            allocations[resource] = state
            owner_id, _, _ = resource
            grants[owner_id] = max(grants.get(owner_id, 0), state.pending_grants)
        observed = (*states, *roots.values())
        errors = tuple(self._disposal.errors.values()) if self._disposal is not None else ()
        return CleanupStatus(
            "closed" if not errors and all(state.state == "closed" for state in observed) else "pending",
            sum(state.pending_readers for state in allocations.values()) + sum(state.pending_readers for state in roots.values()),
            sum(grants.values()) + sum(state.pending_grants for state in roots.values()),
            sum(state.pending_files for state in allocations.values()) + sum(state.pending_files for state in roots.values()),
            tuple(dict.fromkeys((*errors, *(error for state in observed for error in state.errors)))),
            sum(state.pending_leases for state in allocations.values()) + sum(state.pending_leases for state in roots.values()),
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
    group = None
    try:
        for key, ref in refs.items():
            leases[key] = ref.bound_owner.retain(ref)
        resources = tuple((ref.bound_owner.descriptor()["owner_id"], ref.scope_id, ref.name)
                          for ref in refs.values())
        roots = tuple(dict.fromkeys(ref.bound_owner._return_root for ref in refs.values()
                                   if isinstance(ref.bound_owner, _ReturnMemoryContext)))
        group = ResultGroup(node_name, group_id, tuple(consumed_rows), tuple(leases.values()), resources, roots)
        projected = _project_value(value, group, leases)
        if group._disposal is not None:
            group._disposal.commit()
        return projected, group
    except BaseException:
        if group is not None:
            group._finalizer.detach()
            if group._disposal is not None:
                group._disposal.cancel()
        for lease in leases.values():
            lease.cancel_retention()
        raise
