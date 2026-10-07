"""Public result-bundle export for attached workflow execution."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .result_download import (
    _InstalledResultBundle,
    _install_local_result_bundle,
    _validate_destination_parent,
    _verify_existing_local_result_bundle,
)
from .returns import persist_public_return


class _AttachedReturnRun:
    def __init__(self, run_id: str, storage_path: Path, control_dir: Path) -> None:
        self.id = run_id
        self._storage_path = storage_path
        self.control_dir = control_dir


def export_attached_result(
    context: Any,
    value: Any,
    *,
    destination: str | Path,
) -> Any:
    """Serialize the identity claim and installation for one attached context."""
    with context._lock:
        return _export_attached_result_locked(
            context,
            value,
            destination=destination,
        ).load()


def export_attached_result_bundle(
    context: Any,
    value: Any,
    *,
    destination: str | Path,
) -> Path:
    """Install the identical verified attached bundle without hydrating values."""
    with context._lock:
        return _export_attached_result_locked(
            context, value, destination=destination,
        ).destination


def _export_attached_result_locked(
    context: Any,
    value: Any,
    *,
    destination: str | Path,
) -> _InstalledResultBundle:
    """Persist and export the exact return of one successful attached run."""
    if context.terminal_status != "succeeded" or context.run_id is None:
        raise RuntimeError("Only a successful attached execution can be exported.")
    if not isinstance(destination, (str, Path)) or not str(destination):
        raise TypeError("destination must be a non-empty path.")
    storage_path = context._attached_storage_path
    routes = context._attached_provider_routes
    if storage_path is None or routes is None:
        raise RuntimeError("Execution context has no captured result export binding.")
    destination_path = Path(destination).absolute()
    _validate_destination_parent(destination_path)
    expected_digest = context._result_export_digest
    attached_run = _AttachedReturnRun(
        context.run_id,
        Path(storage_path),
        Path(),
    )
    if destination_path.exists() and expected_digest is not None:
        return _verify_existing_local_result_bundle(
            attached_run,
            destination_path,
            expected_digest=expected_digest,
        )
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.return-",
            dir=destination_path.parent,
        )
    )
    try:
        persist_public_return(
            temporary,
            storage_path,
            context.run_id,
            value,
            outcomes=context.execution_outcomes,
            provider_routes=routes,
        )
        attached_run.control_dir = temporary
        result = _install_local_result_bundle(
            attached_run,
            destination_path,
            expected_digest=expected_digest,
        )
        context._remember_result_export_digest(
            result.manifest["digest"]
        )
        return result
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
