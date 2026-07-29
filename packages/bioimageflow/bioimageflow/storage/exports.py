"""Explicit output export orchestration."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Literal

from .models import CacheCorruptionError
from .storage import Storage


class _DestinationStorage(Storage):
    """Read canonical storage while publishing beneath an external output root."""

    def __init__(self, storage_path: str | Path, outputs_root: Path) -> None:
        super().__init__(storage_path)
        self._outputs_root = outputs_root

    @property
    def outputs_root(self) -> Path:
        return self._outputs_root


def _materialize(
    storage: Storage,
    *,
    mode: Literal["pointer", "symlink", "copy", "hardlink"],
    scope: Literal["latest", "runs", "both"],
    run_id: str | None,
) -> list[Path]:
    materialized: list[Path] = []
    if scope in {"latest", "both"}:
        materialized.extend(storage.materialize_latest_outputs(mode))
    if scope in {"runs", "both"}:
        selected_run_id = run_id or storage.latest_success_run_id()
        if selected_run_id is None:
            raise CacheCorruptionError(
                "No successful run view is available for output export."
            )
        materialized.extend(storage.materialize_run_outputs(selected_run_id, mode))
    return materialized


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _validate_external_destination(storage_path: Path, destination: Path) -> None:
    source = storage_path.resolve()
    target = destination.resolve()
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Output export destination must not be inside the source storage root."
        )
    try:
        source.relative_to(target)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Output export destination must not contain the source storage root."
        )


def _export_to_destination(
    storage_path: Path,
    destination: Path,
    *,
    replace: bool,
    mode: Literal["pointer", "symlink", "copy", "hardlink"],
    scope: Literal["latest", "runs", "both"],
    run_id: str | None,
) -> list[Path]:
    destination = Path(os.path.abspath(destination))
    _validate_external_destination(storage_path, destination)
    if destination.exists() or destination.is_symlink():
        if not replace:
            raise FileExistsError(
                f"Output export destination already exists: {destination}"
            )

    selected_run_id = run_id
    if scope in {"runs", "both"} and selected_run_id is None:
        selected_run_id = Storage(storage_path).latest_success_run_id()
        if selected_run_id is None:
            raise CacheCorruptionError(
                "No successful run view is available for output export."
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.backup"
    moved_previous = False
    installed = False
    try:
        temporary.mkdir()
        storage = _DestinationStorage(storage_path, temporary)
        temporary_paths = _materialize(
            storage,
            mode=mode,
            scope=scope,
            run_id=selected_run_id,
        )
        relative_paths = [path.relative_to(temporary) for path in temporary_paths]

        if destination.exists() or destination.is_symlink():
            os.replace(destination, backup)
            moved_previous = True
        try:
            os.replace(temporary, destination)
            installed = True
        except BaseException:
            if moved_previous:
                os.replace(backup, destination)
                moved_previous = False
            raise
        if moved_previous:
            _remove_path(backup)
            moved_previous = False
        return [destination / path for path in relative_paths]
    finally:
        _remove_path(temporary)
        if (
            moved_previous
            and not installed
            and not (destination.exists() or destination.is_symlink())
        ):
            os.replace(backup, destination)
            moved_previous = False
        if not moved_previous:
            _remove_path(backup)


def export_outputs(
    storage_path: str | Path,
    *,
    destination: str | Path | None = None,
    replace: bool = False,
    mode: Literal["pointer", "symlink", "copy", "hardlink"] = "copy",
    scope: Literal["latest", "runs", "both"] = "latest",
    run_id: str | None = None,
) -> list[Path]:
    """Materialize assets, dataframes, and provenance from canonical run views.

    Without ``destination``, outputs are materialized beneath the storage root as
    before. An explicit destination is installed as one complete output root and
    contains ``latest/`` and/or ``runs/<run-id>/`` according to ``scope``.
    """
    if scope not in {"latest", "runs", "both"}:
        raise ValueError("Invalid output scope. Expected 'latest', 'runs', or 'both'.")
    storage_path = Path(storage_path)
    if destination is None:
        if replace:
            raise ValueError("'replace' requires an explicit output destination.")
        return _materialize(
            Storage(storage_path),
            mode=mode,
            scope=scope,
            run_id=run_id,
        )
    return _export_to_destination(
        storage_path,
        Path(destination),
        replace=replace,
        mode=mode,
        scope=scope,
        run_id=run_id,
    )
