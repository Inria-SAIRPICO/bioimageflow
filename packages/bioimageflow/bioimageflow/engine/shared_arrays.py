"""Controller task scopes retained through actual worker-pool drain."""
from __future__ import annotations

import threading
from typing import Any, Iterator

from bioimageflow_core import SharedArray, SharedMemoryContext, collect_input_scopes


def references(value: Any) -> Iterator[SharedArray]:
    if isinstance(value, SharedArray):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from references(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from references(child)


class SharedTaskScope:
    """Own only this task's outputs; inputs keep their original owners."""

    def __init__(self, owner: SharedMemoryContext, identity: str, inputs: Any) -> None:
        descriptors = collect_input_scopes(inputs)
        self.context = owner.task_scope(identity)
        try:
            self.grant = self.context.acquire_worker_grant(tuple(references(inputs)))
        except BaseException:
            self.context.close()
            raise
        self.wire = {"output": self.context.descriptor(), "inputs": list(descriptors)}
        self._disposition = "pending"
        self._drained = False
        self._lock = threading.RLock()

    def borrowed(self) -> SharedMemoryContext:
        return SharedMemoryContext.borrow(self.wire["output"], inputs=self.wire["inputs"])

    def accept_outputs(self, outputs: list[list[Any]]) -> list[list[Any]]:
        # Outputs have already passed declared field validation/correlation.
        values = [[{name: getattr(output, name) for name in output._get_all_annotations()}
                   for output in row] for row in outputs]
        with self._lock:
            if self._disposition == "rejected":
                raise RuntimeError("Task outputs have already been rejected")
            bound = self.context.accept_result(values)
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
