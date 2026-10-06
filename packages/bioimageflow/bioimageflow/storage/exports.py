"""Explicit output export orchestration."""

from __future__ import annotations

import logging
import os
import stat
import shutil
import uuid
from pathlib import Path
from typing import Literal

from bioimageflow.filesystem import publish_no_replace

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


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        found = path.lstat()
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino, stat.S_IFMT(found.st_mode)


def _diagnose(primary: BaseException, message: str) -> None:
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(message)
    logging.getLogger("bioimageflow").warning(message)


def _report_recovery(
    primary: BaseException, backup: Path, identity: tuple[int, int, int], rollback: BaseException,
) -> None:
    # Detached diagnostics retain no storage, reader or execution owner.
    setattr(primary, "export_recovery", {"path": str(backup), "identity": identity})
    _diagnose(primary, f"Export backup remains recoverable at {backup} "
        f"(identity {identity}); restoration refused: {rollback!r}")


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
    admitted = _path_identity(destination)
    if admitted is not None and not replace:
        raise FileExistsError(f"Output export destination already exists: {destination}")

    selected_run_id = run_id
    if scope in {"runs", "both"} and selected_run_id is None:
        selected_run_id = Storage(storage_path).latest_success_run_id()
        if selected_run_id is None:
            raise CacheCorruptionError("No successful run view is available for output export.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.backup"
    temporary_identity = backup_identity = None
    installed = False
    primary = None
    try:
        temporary.mkdir()
        temporary_identity = _path_identity(temporary)
        storage = _DestinationStorage(storage_path, temporary)
        temporary_paths = _materialize(storage, mode=mode, scope=scope, run_id=selected_run_id)
        relative_paths = [path.relative_to(temporary) for path in temporary_paths]

        if admitted is not None:
            if _path_identity(destination) != admitted:
                raise FileExistsError(f"Output export destination owner changed: {destination}")
            publish_no_replace(destination, backup)
            backup_identity = _path_identity(backup)
            if backup_identity != admitted:
                raise FileExistsError(f"Output export destination changed during backup: {destination}")
        # Initial absence and the gap after backup confer no overwrite authority.
        publish_no_replace(temporary, destination)
        installed = True
        if backup_identity is not None:
            if _path_identity(backup) != admitted:
                raise FileExistsError(f"Output export backup owner changed: {backup}")
            _remove_path(backup)
            backup_identity = None
        return [destination / path for path in relative_paths]
    except BaseException as error:
        primary = error
        if backup_identity is not None and not installed:
            try:
                if _path_identity(backup) != backup_identity:
                    raise FileExistsError(f"Output export backup owner changed: {backup}")
                publish_no_replace(backup, destination)
                backup_identity = None
            except BaseException as rollback:
                _report_recovery(error, backup, backup_identity, rollback)
        elif backup_identity is not None and installed:
            setattr(error, "export_cleanup", {
                "installed_destination": str(destination),
                "pending_backup": {"path": str(backup), "identity": backup_identity},
            })
            _diagnose(error, f"Output export was installed at {destination}; superseded backup "
                f"cleanup remains pending at {backup} (identity {backup_identity}). "
                "The backup may be partially deleted; automatic rollback was not attempted.")
        raise
    finally:
        if temporary_identity is not None and _path_identity(temporary) == temporary_identity:
            try:
                _remove_path(temporary)
            except BaseException as cleanup:
                if primary is None:
                    raise
                _diagnose(primary, f"Owned export staging cleanup remains pending at {temporary}: {cleanup!r}")


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
