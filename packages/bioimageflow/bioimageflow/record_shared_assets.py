"""Attempt-local sealed references for an admitted own-winner record."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from bioimageflow_core import SharedArray, SharedMemoryContext
from bioimageflow_core.shared_memory import SharedArrayLease


@dataclass(frozen=True)
class _AcceptedAsset:
    reference: SharedArray
    output: dict[str, Any]
    lease: SharedArrayLease


class RecordSharedAssets:
    """Pin exact emitted sealed leaves without copying or hashing pixels."""

    def __init__(self) -> None:
        self._assets: dict[str, _AcceptedAsset] = {}

    def __enter__(self) -> RecordSharedAssets:
        return self

    def __exit__(self, exc_type: Any, primary: BaseException | None, traceback: Any) -> None:
        try:
            self.close()
        except Exception as cleanup:
            error = cleanup if primary is None else primary
            setattr(error, "_record_shared_assets_cleanup", self)
            if primary is None:
                raise
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                add_note(f"Shared-array receipt pin cleanup remains pending: {cleanup!r}")

    def capture(self, reference: SharedArray, output: dict[str, Any]) -> None:
        owner = reference.bound_owner
        if not isinstance(owner, SharedMemoryContext):
            return
        try:
            lease = owner.retain(reference)
        except (ValueError, RuntimeError):
            # Mutable or closing inputs retain exact hydration.
            return
        path = output["path"]
        if path in self._assets:
            from bioimageflow.storage.models import CacheCorruptionError

            lease.cancel_retention()
            raise CacheCorruptionError("Duplicate emitted shared-array receipt")
        self._assets[path] = _AcceptedAsset(reference, deepcopy(output), lease)

    def resolve(self, output: dict[str, Any], *, column: str, row_index: str) -> SharedArray | None:
        receipt = self._assets.get(output["path"])
        if receipt is None:
            return None
        reference = receipt.reference
        facts = output["array"]
        if (output != receipt.output or facts["column"] != column
                or facts["row_index"] != row_index or facts["shape"] != list(reference.shape)
                or facts["dtype"] != reference.dtype):
            from bioimageflow.storage.models import CacheCorruptionError

            raise CacheCorruptionError("Selected shared-array asset disagrees with its emitted receipt")
        return reference

    def close(self) -> None:
        failures = []
        for path, receipt in tuple(self._assets.items()):
            try:
                receipt.lease.cancel_retention()
                del self._assets[path]
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]
