"""Controller task scopes retained through actual worker-pool drain."""
from __future__ import annotations

import threading
from typing import Any, Iterator

from bioimageflow_core import SharedArray, SharedMemoryContext, collect_input_scopes, accept_native_array
from bioimageflow.result_groups import map_shared_values


def references(value: Any) -> Iterator[SharedArray]:
    if isinstance(value, SharedArray):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from references(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from references(child)


def publish_inputs(value: Any) -> Any:
    """Snapshot each producer once per admission; reuse already accepted refs."""
    import pandas as pd
    import numpy as np
    native: dict[int, np.ndarray] = {}
    memo: dict[tuple[Any, ...], SharedArray] = {}
    created: list[SharedArray] = []

    def walk(item: Any) -> Any:
        if isinstance(item, np.ndarray):
            if id(item) not in native:
                native[id(item)] = accept_native_array(item)
            return accept_native_array(native[id(item)])
        if isinstance(item, SharedArray):
            owner = item.bound_owner
            if not isinstance(owner, SharedMemoryContext):
                raise ValueError("Shared input requires its admitted controller owner")
            key = (id(owner), item.scope_id, item.name, item.shape, item.dtype)
            if key not in memo:
                memo[key] = owner.publish(item)
                if memo[key] != item:
                    created.append(memo[key])
            return memo[key]
        if isinstance(item, pd.DataFrame):
            return map_shared_values(item, walk)
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        return item

    try:
        return walk(value)
    except BaseException:
        for ref in created:
            ref.bound_owner.release(ref)
        raise




class SharedTaskScope:
    """Own only this task's outputs; inputs keep their original owners."""

    def __init__(self, owner: SharedMemoryContext, identity: str, inputs: Any) -> None:
        self.inputs = publish_inputs(inputs)
        descriptors = collect_input_scopes(self.inputs)
        self.context = owner.task_scope(identity)
        try:
            self.grant = self.context.acquire_worker_grant(tuple(references(self.inputs)))
            self.inputs = self.context.bind_value(self.inputs)
        except BaseException:
            self.context.close()
            raise
        self.wire = {"output": self.context.descriptor(), "inputs": list(descriptors)}
        self._disposition = "pending"
        self._drained = False
        self._lock = threading.RLock()

    def borrowed(self) -> SharedMemoryContext:
        return SharedMemoryContext.borrow(self.wire["output"], inputs=self.wire["inputs"])

    def publish_outputs(self, outputs: list[list[Any]]) -> list[list[Any]]:
        """Capture validated callback values before another callback can mutate them."""
        values = [[{name: getattr(output, name) for name in output._get_all_annotations()}
                   for output in row] for row in outputs]
        with self._lock:
            if self._disposition == "rejected":
                raise RuntimeError("Task outputs have already been rejected")
            bound = self.context.accept_result(values)
            return [[type(output)(**value) for output, value in zip(row, bound_row, strict=True)]
                    for row, bound_row in zip(outputs, bound, strict=True)]

    def accept_outputs(self, outputs: list[list[Any]]) -> list[list[Any]]:
        # Final acceptance retains one result group after validation/correlation.
        with self._lock:
            published = self.publish_outputs(outputs)
            values = [[{name: getattr(output, name) for name in output._get_all_annotations()}
                       for output in row] for row in published]
            from bioimageflow.result_groups import bind_result_group
            bound, _group = bind_result_group(values, node_name="task", group_id=self.context.scope_id)
            restored = [[type(output)(**value) for output, value in zip(row, bound_row, strict=True)]
                        for row, bound_row in zip(outputs, bound, strict=True)]
            self._disposition = "accepted"
            if self._drained:
                self.context.discard_unreturned()
            return restored

    def fail(self) -> None:
        with self._lock:
            self._disposition = "rejected"
            self.context.close()

    def drained(self) -> None:
        """Physical worker retirement is independent of controller disposition."""
        with self._lock:
            if self._drained:
                return
            self.grant.drained()
            self._drained = True
            if self._disposition == "accepted":
                self.context.discard_unreturned()
            elif self._disposition == "rejected":
                self.context.close()
            # Pending outputs remain until controller acceptance or rejection.
