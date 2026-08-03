"""Public result-bundle export for attached workflow execution."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .inputs import LoadedInvocation
from .result_download import (
    _load_existing_local_result,
    _load_manifest,
    _validate_destination_parent,
    export_local_result,
)
from .return_routes import build_return_provider_routes
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
        )


def _export_attached_result_locked(
    context: Any,
    value: Any,
    *,
    destination: str | Path,
) -> Any:
    """Persist and export the exact return of one successful attached run."""
    if context.terminal_status != "succeeded" or context.run_id is None:
        raise RuntimeError("Only a successful attached execution can be exported.")
    if not isinstance(destination, (str, Path)) or not str(destination):
        raise TypeError("destination must be a non-empty path.")
    workflow = context._binding
    if workflow is None or not hasattr(workflow, "storage_path"):
        raise RuntimeError("Execution context has no retained workflow binding.")
    target_nodes = context._target_nodes
    if type(target_nodes) is not tuple or any(
        type(node) is not str or not node for node in target_nodes
    ):
        raise RuntimeError("Execution context has no retained target binding.")
    invocation = LoadedInvocation(
        variant="targets",
        inputs=MappingProxyType({}),
        targets=target_nodes,
        outputs=(),
    )
    destination_path = Path(destination).absolute()
    _validate_destination_parent(destination_path)
    expected_digest = context._result_export_digest
    attached_run = _AttachedReturnRun(
        context.run_id,
        Path(workflow.storage_path),
        Path(),
    )
    if destination_path.exists() and expected_digest is not None:
        return _load_existing_local_result(
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
            workflow.storage_path,
            context.run_id,
            value,
            outcomes=context.execution_outcomes,
            provider_routes=build_return_provider_routes(
                workflow,
                invocation,
                context.execution_outcomes,
            ),
        )
        attached_run.control_dir = temporary
        result = export_local_result(
            attached_run,
            destination_path,
            expected_digest=expected_digest,
        )
        context._remember_result_export_digest(
            _load_manifest(destination_path / "manifest.json")["digest"]
        )
        return result
    finally:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
