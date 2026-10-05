"""Explicit controller ownership for numeric, file-backed shared-array scopes.

Borrowed contexts never delete storage. Completion of an execution only unbinds
the context; the caller explicitly requests release, and readers/worker grants
keep pending backing alive until their physical lifetime has drained.
"""

import contextvars
import json
import shutil
import threading
import uuid
import weakref
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Optional

from bioimageflow_core import _shared_storage as storage
from bioimageflow_core.types import SharedArray

_active: contextvars.ContextVar[Optional["SharedMemoryContext"]] = contextvars.ContextVar(
    "bioimageflow_shared_memory_context", default=None
)


@dataclass(frozen=True)
class CleanupStatus:
    """A truthful physical-cleanup snapshot, never a terminal-state guess."""

    state: str
    pending_readers: int
    pending_grants: int
    pending_files: int
    errors: tuple[str, ...] = ()
    pending_leases: int = 0


class _Owner:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.contexts: dict[str, SharedMemoryContext] = {}
        self.readers: dict[tuple[str, str], int] = {}
        self.grants = 0
        self.leases: dict[tuple[str, str], int] = {}
        self.lease_release_requested: set[tuple[str, str]] = set()
        self.closing: set[str] = set()
        self.released: set[tuple[str, str]] = set()
        self.errors: dict[tuple[str, str], str] = {}
        self.retired: set[str] = set()
        self.retiring: set[str] = set()


def _check_ref_access(ref: SharedArray) -> None:
    if ref._lease is not None and ref._lease.released:
        raise RuntimeError("This result group has been released")


def _release_lease(context: "SharedMemoryContext", key: tuple[str, str], state: dict[str, bool]) -> CleanupStatus:
    assert context._owner is not None
    with context._owner.lock:
        if not state["released"]:
            state["released"] = True
            if state["retire"]:
                context._owner.lease_release_requested.add(key)
            count = context._owner.leases[key] - 1
            if count:
                context._owner.leases[key] = count
            else:
                context._owner.leases.pop(key)
                if key in context._owner.lease_release_requested:
                    context._owner.released.add(key)
            allocation_context = context._owner.contexts[key[0]]
            allocation_context._cleanup()
            allocation_context._retire_namespaces()
        return context._owner.contexts[key[0]]._status(key)


class SharedArrayLease:
    """One exact accepted allocation retention; never retains a descriptor/group."""

    def __init__(self, context: "SharedMemoryContext", ref: SharedArray) -> None:
        self._context = context
        self._metadata = ref.name, ref.shape, ref.dtype, ref.scope_id
        self._state = {"released": False, "retire": True}
        self._finalizer = weakref.finalize(self, _release_lease, context,
                                          (ref.scope_id, ref.name), self._state)

    @property
    def released(self) -> bool:
        return self._state["released"]

    def project(self, group: Any) -> SharedArray:
        if self.released:
            raise RuntimeError("This result lease has been released")
        name, shape, dtype, scope_id = self._metadata
        return SharedArray(name, shape, dtype, scope_id, self._context, self, group)

    def owns(self, ref: SharedArray) -> bool:
        """Verify this local lease projection without consulting backing storage."""
        return (ref._lease is self and ref.bound_owner is self._context
                and (ref.name, ref.shape, ref.dtype, ref.scope_id) == self._metadata)

    def release(self) -> CleanupStatus:
        assert self._context._owner is not None
        with self._context._owner.lock:
            if self._finalizer.alive:
                result = self._finalizer()
                assert isinstance(result, CleanupStatus)
                return result
            return self.status()

    def cancel_retention(self) -> CleanupStatus:
        """Undo an unpublished admission pin without requesting allocation deletion."""
        assert self._context._owner is not None
        with self._context._owner.lock:
            self._state["retire"] = False
            return self.release()

    def status(self) -> CleanupStatus:
        assert self._context._owner is not None
        with self._context._owner.lock:
            return self._context._owner.contexts[self._metadata[3]]._status((self._metadata[3], self._metadata[0]))


class WorkerGrant:
    """An admission retained until the controller proves physical retirement."""

    def __init__(self, owners: list[_Owner]) -> None:
        self._owners = owners
        self._lock = threading.Lock()
        for owner in owners:
            with owner.lock:
                owner.grants += 1

    def drained(self) -> None:
        """Release this exact admission only after successful resource drain."""
        with self._lock:
            owners, self._owners = self._owners, []
        for owner in owners:
            with owner.lock:
                owner.grants -= 1
                for context in tuple(owner.contexts.values()):
                    if not owner.grants and (context._inputs_settled or context.scope_id in owner.closing):
                        context._clear_input_refs()
                    context._cleanup()
                next(iter(owner.contexts.values()))._retire_namespaces()


def validate_scope_descriptor(value: Any) -> dict[str, Any]:
    """Pure current-descriptor validation; no path resolution or file access."""
    return storage.validate_scope_descriptor(value)


def get_shared_memory_context() -> "SharedMemoryContext":
    context = _active.get()
    if context is None:
        raise RuntimeError("Shared array allocation requires an explicit SharedMemoryContext")
    return context


def _refs(value: Any) -> Iterator[SharedArray]:
    if isinstance(value, SharedArray):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _refs(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _refs(child)


def collect_input_scopes(value: Any) -> tuple[dict[str, Any], ...]:
    """Collect trusted bound input descriptors without consulting storage."""
    scopes = {}
    for ref in _refs(value):
        _check_ref_access(ref)
        if not isinstance(ref.bound_owner, SharedMemoryContext):
            raise ValueError("Shared array input requires a bound scope owner")
        descriptor = ref.bound_owner._descriptor_for(ref.scope_id)
        scopes[ref.scope_id] = descriptor
    return tuple(scopes.values())


class SharedMemoryContext:
    """Controller-owned scope or explicitly admitted borrowed task namespace."""

    def __init__(self, root: Any, max_bytes: Optional[int] = None,
                 max_header_bytes: int = 10000) -> None:
        parent = Path(root).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        budget = shutil.disk_usage(parent).free if max_bytes is None else max_bytes
        if type(budget) is not int or budget <= 0 or type(max_header_bytes) is not int or max_header_bytes <= 0:
            raise ValueError("Shared scope budgets must be finite positive integers")
        scope_id = uuid.uuid4().hex
        scope_root = parent / ("bif_shared_" + scope_id)
        scope_root.mkdir(mode=0o700)
        self._descriptor = {
            "scope_id": scope_id, "root": str(scope_root),
            "root_identity": storage.identity(scope_root), "owner_id": scope_id,
            "owner_root": str(scope_root), "owner_root_identity": storage.identity(scope_root),
            "max_bytes": budget, "max_header_bytes": max_header_bytes,
        }
        self._write_marker()
        (scope_root / ".quota.lock").write_bytes(b"0")
        (scope_root / ".quota.json").write_text("{}", encoding="utf-8")
        self._owner: Optional[_Owner] = _Owner()
        self._owner.contexts[scope_id] = self
        self._inputs: dict[str, dict[str, Any]] = {}
        self._input_refs: dict[tuple[str, str, tuple[int, ...], str], SharedArray] = {}
        self._accepted: set[str] = set()
        self._inputs_settled = False

    def _write_marker(self) -> None:
        path = Path(self._descriptor["root"]) / ".scope.json"
        path.write_text(json.dumps({"scope_id": self._descriptor["scope_id"],
                                   "owner_id": self._descriptor["owner_id"]}), encoding="utf-8")

    @classmethod
    def borrow(cls, descriptor: Any, inputs: Any = ()) -> "SharedMemoryContext":
        """Admit controller-provided descriptors without storage I/O/deletion authority."""
        result = cls.__new__(cls)
        result._descriptor = validate_scope_descriptor(descriptor)
        result._owner = None
        result._inputs = {}
        for value in inputs:
            admitted = validate_scope_descriptor(value)
            if admitted["scope_id"] == result.scope_id and admitted != result._descriptor:
                raise ValueError("Conflicting admitted output scope identity")
            existing = result._inputs.get(admitted["scope_id"])
            if existing is not None and existing != admitted:
                raise ValueError("Conflicting admitted scope identity")
            result._inputs[admitted["scope_id"]] = admitted
        result._input_refs = {}
        result._accepted = set()
        result._inputs_settled = False
        return result

    def descriptor(self) -> dict[str, Any]:
        return validate_scope_descriptor(self._descriptor)

    @property
    def scope_id(self) -> str:
        return self._descriptor["scope_id"]

    def _descriptor_for(self, scope_id: str) -> dict[str, Any]:
        if scope_id == self.scope_id:
            return self.descriptor()
        if scope_id in self._inputs:
            return validate_scope_descriptor(self._inputs[scope_id])
        if self._owner is not None and scope_id in self._owner.contexts:
            return self._owner.contexts[scope_id].descriptor()
        raise ValueError("Shared reference scope is not admitted")

    @contextmanager
    def activate(self) -> Iterator["SharedMemoryContext"]:
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    def __enter__(self) -> "SharedMemoryContext":
        self._activation = self.activate()
        return self._activation.__enter__()

    def __exit__(self, *args: Any) -> Any:
        return self._activation.__exit__(*args)

    def _require_open(self, scope_id: Optional[str] = None) -> None:
        if self._owner is not None and (scope_id or self.scope_id) in self._owner.closing:
            raise RuntimeError("Shared scope is closing; new controller access is refused")

    def task_scope(self, task_id: str) -> "SharedMemoryContext":
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Task namespace requires an invocation identity")
        if self._owner is None:
            raise RuntimeError("Only a controller can admit task namespaces")
        with self._owner.lock:
            self._require_open()
            result = self.__class__.__new__(self.__class__)
            scope_id = uuid.uuid4().hex
            root = Path(self._descriptor["owner_root"]) / ("task_" + scope_id)
            root.mkdir(mode=0o700)
            result._descriptor = self.descriptor()
            result._descriptor.update(scope_id=scope_id, root=str(root), root_identity=storage.identity(root))
            result._owner = self._owner
            result._inputs, result._input_refs, result._accepted = {}, {}, set()
            result._inputs_settled = False
            result._write_marker()
            self._owner.contexts[scope_id] = result
            return result

    @staticmethod
    def _ref_key(ref: SharedArray) -> tuple[str, str, tuple[int, ...], str]:
        return ref.scope_id, ref.name, ref.shape, ref.dtype

    def acquire_worker_grant(self, inputs: Any = ()) -> WorkerGrant:
        if self._owner is None:
            raise RuntimeError("Borrowed scopes cannot issue grants")
        refs = tuple(_refs(inputs))
        owners = [self._owner]
        for ref in refs:
            context = ref.bound_owner
            if not isinstance(context, SharedMemoryContext) or context._owner is None:
                raise ValueError("Worker grant requires controller-bound inputs")
            if context._owner not in owners:
                owners.append(context._owner)
        # Admission and all captured owner increments form one atomic lifetime
        # boundary, also when two tasks exchange each other's input scopes.
        with ExitStack() as locks:
            for owner in sorted(owners, key=id):
                locks.enter_context(owner.lock)
            self._require_open()
            for ref in refs:
                _check_ref_access(ref)
                context = ref.bound_owner
                context._require_open(ref.scope_id)
                if (ref.scope_id, ref.name) in context._owner.released:
                    raise RuntimeError("Shared input allocation is closing")
                context._descriptor_for(ref.scope_id)
            staged: dict[Any, SharedArray] = {}
            leases: list[SharedArrayLease] = []
            try:
                for ref in refs:
                    key = self._ref_key(ref)
                    if key in self._input_refs or key in staged:
                        continue
                    context = ref.bound_owner
                    descriptor = context._descriptor_for(ref.scope_id)
                    if storage.sealed(descriptor, ref):
                        lease = context.retain(ref)
                        leases.append(lease)
                        staged[key] = lease.project(None)
                    else:
                        staged[key] = ref
            except BaseException:
                for lease in leases:
                    lease.cancel_retention()
                raise
            for key, ref in staged.items():
                self._inputs[ref.scope_id] = ref.bound_owner._descriptor_for(ref.scope_id)
                self._input_refs[key] = ref

            return WorkerGrant(owners)

    def _clear_input_refs(self) -> None:
        refs, self._input_refs = self._input_refs, {}
        for ref in refs.values():
            if ref._lease is not None and ref.bound_group is None:
                ref._lease.release()

    def bind(self, ref: SharedArray) -> SharedArray:
        storage.token(ref.name)
        self._descriptor_for(ref.scope_id)
        original = self._input_refs.get(self._ref_key(ref))
        if original is not None:
            return original
        return replace(ref, _owner=self)

    def bind_value(self, value: Any) -> Any:
        if isinstance(value, SharedArray):
            return self.bind(value)
        if isinstance(value, dict):
            return {key: self.bind_value(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.bind_value(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.bind_value(child) for child in value)
        return value

    def accept_result(self, value: Any) -> Any:
        for ref in _refs(value):
            if ref.scope_id != self.scope_id and self._ref_key(ref) not in self._input_refs:
                raise ValueError("Task result contains an unadmitted shared reference")
        result = self.publish_value(self.bind_value(value))
        self._accepted.update(ref.name for ref in _refs(result) if ref.scope_id == self.scope_id)
        return result

    def publish(self, ref: SharedArray) -> SharedArray:
        """Create one independent accepted snapshot; accepted inputs are reused."""
        return self.publish_value(ref)

    def publish_value(self, value: Any) -> Any:
        """Deduplicate publication within this admission, without global mutable caches."""
        if self._owner is None:
            raise RuntimeError("Only a controller can publish accepted backing")
        memo: dict[Any, SharedArray] = {}
        created: list[SharedArray] = []

        def publish(ref: SharedArray) -> SharedArray:
            _check_ref_access(ref)
            source = ref.bound_owner
            if not isinstance(source, SharedMemoryContext):
                raise ValueError("Publication requires a bound source owner")
            descriptor = source._descriptor_for(ref.scope_id)
            if storage.sealed(descriptor, ref):
                return ref
            key = self._ref_key(ref)
            if key not in memo:
                array = source.open(ref, writable=False)
                try:
                    result = self.create(array)
                    created.append(result)
                    storage.seal(self._descriptor_for(result.scope_id), result)
                    memo[key] = result
                finally:
                    del array
            return memo[key]

        def walk(item: Any) -> Any:
            if isinstance(item, SharedArray):
                return publish(item)
            if isinstance(item, dict):
                return {key: walk(child) for key, child in item.items()}
            if isinstance(item, list):
                return [walk(child) for child in item]
            if isinstance(item, tuple):
                return tuple(walk(child) for child in item)
            return item

        with self._owner.lock:
            self._require_open()
            try:
                result = walk(value)
            except BaseException:
                for ref in created:
                    self.release(ref)
                raise
            self._accepted.update(ref.name for ref in created)
            return result

    def content_identity(self, ref: SharedArray) -> dict[str, Any]:
        """Accepted digest or a mutable planning preview, never a producer stability promise."""
        _check_ref_access(ref)
        if ref.bound_owner is not self or self._owner is None:
            raise ValueError("Content identity requires the exact bound controller owner")
        with self._owner.lock:
            self._require_open(ref.scope_id)
            if (ref.scope_id, ref.name) in self._owner.released:
                raise RuntimeError("Shared allocation is closing")
            return storage.content_identity(self._descriptor_for(ref.scope_id), ref)

    def retain(self, ref: SharedArray) -> SharedArrayLease:
        """Retain one sealed allocation without retaining its descriptor."""
        _check_ref_access(ref)
        context = ref.bound_owner
        if self._owner is None or not isinstance(context, SharedMemoryContext) or context._owner is not self._owner:
            raise ValueError("Retention requires the exact controller-bound owner")
        with self._owner.lock:
            context._require_open(ref.scope_id)
            key = ref.scope_id, ref.name
            if key in self._owner.released or not storage.sealed(context._descriptor_for(ref.scope_id), ref):
                raise ValueError("Only an open accepted allocation can be retained")
            self._owner.leases[key] = self._owner.leases.get(key, 0) + 1
            return SharedArrayLease(context, ref)

    def create(self, data: Any, name: Optional[str] = None) -> SharedArray:
        import numpy as np
        array = np.asarray(data)
        storage.dtype(array.dtype)  # refuse object fields before any backing effect
        name = storage.token(name) if name is not None else "bif_" + uuid.uuid4().hex
        if self._owner is None:
            storage.create(self._descriptor, name, array)
        else:
            with self._owner.lock:
                self._require_open()
                storage.create(self._descriptor, name, array)
        return self.bind(SharedArray(name, array.shape, str(array.dtype), self.scope_id))

    def open(self, ref: SharedArray, *, writable: Optional[bool] = None) -> Any:
        _check_ref_access(ref)
        if writable is not None and type(writable) is not bool:
            raise TypeError("writable must be bool or None")
        storage.dtype(ref.dtype)  # before any file admission
        descriptor = self._descriptor_for(ref.scope_id)
        if self._owner is not None:
            key = (ref.scope_id, ref.name)
            with self._owner.lock:
                self._require_open(ref.scope_id)
                if key in self._owner.released:
                    raise RuntimeError("Shared allocation is closing")
                array, mapping = storage.map_array(descriptor, ref, writable=writable)
                self._owner.readers[key] = self._owner.readers.get(key, 0) + 1
        else:
            array, mapping = storage.map_array(descriptor, ref, writable=writable)
        # No callback captures array; every view keeps its public base ndarray.
        weakref.finalize(array, self._reader_drained, mapping, ref.scope_id, ref.name)
        return array

    def _reader_drained(self, mapping: Any, scope_id: str, name: str) -> None:
        mapping.close()
        if self._owner is not None:
            with self._owner.lock:
                key = scope_id, name
                self._owner.readers[key] -= 1
                if not self._owner.readers[key]:
                    self._owner.readers.pop(key)
                for context in tuple(self._owner.contexts.values()):
                    context._cleanup()
                self._retire_namespaces()

    def _allocations(self) -> list[str]:
        if self._owner is not None and self.scope_id in self._owner.retired | self._owner.retiring:
            return []
        with storage.quota(self._descriptor) as ledger:
            return [key.split(":")[1] for key in ledger if key.split(":")[0] == self.scope_id]

    def _cleanup(self) -> None:
        if self._owner is None or self._owner.grants or self.scope_id in self._owner.retired:
            return
        try:
            allocations = self._allocations()
            self._owner.errors.pop((self.scope_id, "ledger"), None)
        except (OSError, ValueError) as error:
            self._owner.errors[(self.scope_id, "ledger")] = str(error)
            return
        for name in allocations:
            key = self.scope_id, name
            if self.scope_id not in self._owner.closing and key not in self._owner.released:
                continue
            if self._owner.readers.get(key) or self._owner.leases.get(key):
                continue
            try:
                storage.delete(self._descriptor, name)
                self._owner.errors.pop(key, None)
            except (OSError, ValueError) as error:
                self._owner.errors[key] = str(error)

    def _retire_namespaces(self) -> None:
        if self._owner is None or self._owner.grants or self._owner.readers or self._owner.leases:
            return
        # Children precede the owner root; never recurse through foreign entries.
        contexts = sorted(self._owner.contexts.values(), key=lambda c: c.scope_id == c._descriptor["owner_id"])
        for context in contexts:
            scope_id = context.scope_id
            if scope_id in self._owner.retired:
                continue
            if scope_id not in self._owner.closing and not (context._inputs_settled
                    and scope_id != context._descriptor["owner_id"]):
                continue
            try:
                remaining_allocations = context._allocations()
            except (OSError, ValueError) as error:
                self._owner.errors[(scope_id, "ledger")] = str(error)
                continue
            if remaining_allocations:
                continue
            self._owner.closing.add(scope_id)
            root = Path(context._descriptor["root"])
            expected = {".scope.json"}
            if scope_id == context._descriptor["owner_id"]:
                if any(c.scope_id not in self._owner.retired for c in contexts if c is not context):
                    continue
                expected.update((".quota.lock", ".quota.json"))
                expected.add(".quota.tmp")
            try:
                if scope_id not in self._owner.retiring:
                    storage.verify(context._descriptor)
                elif storage.identity(root) != context._descriptor["root_identity"]:
                    raise ValueError("Shared scope root identity changed during cleanup")
                remaining = {p.name for p in root.iterdir()}
                if not remaining <= expected:
                    raise ValueError("Shared scope contains an unowned entry; cleanup deferred")
                self._owner.retiring.add(scope_id)
                for name in sorted(remaining, key=lambda name: name == ".scope.json"):
                    (root / name).unlink()
                root.rmdir()
                self._owner.retired.add(scope_id)
                self._owner.retiring.discard(scope_id)
                self._owner.errors.pop((scope_id, "namespace"), None)
            except (OSError, ValueError) as error:
                self._owner.errors[(scope_id, "namespace")] = str(error)

    def _status(self, only: Optional[tuple[str, str]] = None) -> CleanupStatus:
        if self._owner is None:
            raise RuntimeError("Borrowed contexts have no deletion ownership")
        contexts = (self,) if only is not None or self.scope_id != self._descriptor["owner_id"] else tuple(self._owner.contexts.values())
        files = 0
        for context in contexts:
            try:
                files += len(context._allocations())
            except (OSError, ValueError) as error:
                self._owner.errors[(context.scope_id, "ledger")] = str(error)
        readers = sum(count for key, count in self._owner.readers.items()
                      if (key == only if only is not None else key[0] in {c.scope_id for c in contexts}))
        if only is not None:
            try:
                files = int(only[1] in self._allocations())
            except (OSError, ValueError) as error:
                self._owner.errors[(self.scope_id, "ledger")] = str(error)
        scope_ids = {context.scope_id for context in contexts}
        if only is not None:
            errors = tuple(error for key, error in self._owner.errors.items()
                           if key == only or key == (only[0], "ledger"))
        else:
            errors = tuple(error for key, error in self._owner.errors.items() if key[0] in scope_ids)
        leases = sum(count for key, count in self._owner.leases.items()
                     if (key == only if only is not None else key[0] in scope_ids))
        return CleanupStatus("pending" if files or readers or self._owner.grants or leases or errors else "closed",
                             readers, self._owner.grants, files, errors, leases)

    def status(self) -> CleanupStatus:
        if self._owner is None:
            raise RuntimeError("Borrowed contexts have no deletion ownership")
        with self._owner.lock:
            for context in tuple(self._owner.contexts.values()):
                context._cleanup()
            self._retire_namespaces()
            return self._status()

    def release(self, ref: SharedArray) -> CleanupStatus:
        if self._owner is None or not isinstance(ref.bound_owner, SharedMemoryContext):
            raise ValueError("Only a bound controller owner can release a reference")
        storage.token(ref.name)
        if ref.scope_id not in self._owner.contexts or ref.bound_owner._owner is not self._owner:
            raise ValueError("Shared reference is not owned by this controller")
        with self._owner.lock:
            key = ref.scope_id, ref.name
            self._owner.released.add(key)
            context = self._owner.contexts[ref.scope_id]
            context._cleanup()
            return context._status(key)

    def close(self) -> CleanupStatus:
        if self._owner is None:
            raise RuntimeError("Borrowed contexts cannot release controller storage")
        with self._owner.lock:
            contexts = tuple(self._owner.contexts.values()) if self.scope_id == self._descriptor["owner_id"] else (self,)
            self._owner.closing.update(context.scope_id for context in contexts)
            for context in contexts:
                if not self._owner.grants:
                    context._clear_input_refs()
                context._cleanup()
            self._retire_namespaces()
            return self._status()

    def discard_unreturned(self) -> CleanupStatus:
        if self._owner is None:
            raise RuntimeError("Borrowed contexts cannot discard controller storage")
        with self._owner.lock:
            self._inputs_settled = True
            self._owner.released.update((self.scope_id, name) for name in self._allocations()
                                        if name not in self._accepted)
            self._cleanup()
            if not self._owner.grants:
                self._clear_input_refs()
            return self._status()
