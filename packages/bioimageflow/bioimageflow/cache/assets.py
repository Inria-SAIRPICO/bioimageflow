"""Focused cache operations for assets."""

from __future__ import annotations

from .common import (
    Any,
    CacheCorruptionError,
    Iterable,
    Path,
    asset_digest_and_size,
    canonical_scalar_payload,
    hashlib,
    os,
    pd,
    re,
    validate_relative_posix_path,
)


def _safe_asset_segment(value: Any) -> str:
    raw = str(value)
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-") or "value"
    sanitized = sanitized[:48]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


def _write_shared_array_asset(
    value: Any,
    *,
    column: str,
    row_index: Any,
    row_position: int,
    staging_assets_dir: Path,
) -> tuple[str, dict[str, Any], Path]:
    from bioimageflow_core.shm import open_shared_array
    from bioimageflow_core.types import SharedArray

    if not isinstance(value, SharedArray):
        raise CacheCorruptionError(
            f"Shared-array output column contains unsupported value: {type(value).__name__}"
        )

    try:
        import numpy as np

        with open_shared_array(value) as source:
            array = np.array(source, copy=True, order="C")
    except Exception as exc:
        raise CacheCorruptionError(
            f"Shared-array output could not be opened for column {column!r}."
        ) from exc

    column_segment = _safe_asset_segment(column)
    row_segment = _safe_asset_segment(row_index)
    relative = validate_relative_posix_path(
        f"assets/shm/{column_segment}/{row_position:06d}_{row_segment}.npy"
    )
    path = (
        staging_assets_dir
        / "shm"
        / column_segment
        / f"{row_position:06d}_{row_segment}.npy"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, array, allow_pickle=False)
    size, digest = asset_digest_and_size(path)
    entry = {
        "array": {
            "column": str(column),
            "dtype": str(array.dtype),
            "format": "npy",
            "order": "C",
            "row_index": str(row_index),
            "shape": list(array.shape),
        },
        "asset_role": "shared_array",
        "asset_type": "file",
        "digest": digest,
        "kind": "owned_asset",
        "path": relative,
        "size": size,
    }
    return relative, entry, path


def _add_processing_owned_asset(
    *,
    path: Path,
    staging_root: Path,
    outputs: list[dict[str, Any]],
    owned_assets: dict[str, Path],
    seen_outputs: set[tuple[str, str]],
    output_column: str | None = None,
    row_index: Any | None = None,
    require_exists: bool = True,
) -> str | None:
    try:
        relative = path.resolve().relative_to(staging_root)
    except ValueError as exc:
        raise CacheCorruptionError(
            f"Declared owned output asset is outside staging assets: {path}"
        ) from exc
    try:
        record_relative = validate_relative_posix_path(f"assets/{relative.as_posix()}")
    except ValueError as exc:
        raise CacheCorruptionError("Declared output asset path is unsafe.") from exc
    if record_relative.startswith("assets/shm/"):
        raise CacheCorruptionError("assets/shm/ is reserved for shared-array assets.")
    if not path.exists():
        if require_exists:
            raise CacheCorruptionError(f"Declared output asset is missing: {path}")
        return None
    try:
        path.resolve().relative_to(staging_root)
    except ValueError as exc:
        raise CacheCorruptionError(
            f"Declared output asset escapes staging assets: {path}"
        ) from exc
    size, digest = asset_digest_and_size(path)
    previous = owned_assets.get(record_relative)
    if previous is not None and previous.resolve() != path.resolve():
        raise CacheCorruptionError(f"Duplicate owned asset path: {record_relative}")
    for existing_relative, existing_path in owned_assets.items():
        if existing_relative == record_relative:
            continue
        if record_relative.startswith(
            f"{existing_relative}/"
        ) or existing_relative.startswith(f"{record_relative}/"):
            raise CacheCorruptionError(
                f"Overlapping owned asset paths are not supported: {existing_relative}, {record_relative}"
            )
    owned_assets[record_relative] = path
    entry_key = ("owned_asset", record_relative)
    if entry_key not in seen_outputs:
        entry = {
            "path": record_relative,
            "kind": "owned_asset",
            "asset_type": "directory" if path.is_dir() else "file",
            "size": size,
            "digest": digest,
        }
        if output_column is not None:
            entry["output_column"] = str(output_column)
        if row_index is not None:
            entry["row_index"] = str(row_index)
        outputs.append(entry)
        seen_outputs.add(entry_key)
    return record_relative


def _add_processing_scalar_output(
    *,
    output_column: str,
    row_index: Any,
    value: Any,
    outputs: list[dict[str, Any]],
    seen_outputs: set[tuple[str, str, str]],
) -> None:
    column = str(output_column)
    index = str(row_index)
    entry_key = ("scalar_output", column, index)
    payload = canonical_scalar_payload(value)
    if entry_key in seen_outputs:
        raise CacheCorruptionError(
            f"Duplicate scalar output metadata: {column} at row {index}"
        )
    outputs.append(
        {
            "kind": "scalar_output",
            "output_column": column,
            "row_index": index,
            "value": payload,
        }
    )
    seen_outputs.add(entry_key)


def _processing_manifest_entries_and_dataframe(
    df: pd.DataFrame,
    path_columns: set[str],
    owned_path_columns: set[str],
    staging_assets_dir: Path,
    shared_array_columns: set[str] | None = None,
    declared_owned_artifact_paths: Iterable[tuple[str, Any, str | os.PathLike[str]]]
    | None = None,
    declared_scalar_outputs: Iterable[tuple[str, Any, Any]] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Path]]:
    stored = df.copy()
    outputs: list[dict[str, Any]] = []
    owned_assets: dict[str, Path] = {}
    seen_outputs: set[tuple[str, str]] = set()
    seen_scalar_outputs: set[tuple[str, str, str]] = set()
    staging_root = staging_assets_dir.resolve()
    shared_array_columns = shared_array_columns or set()
    from bioimageflow_core.types import SharedArray

    for column in shared_array_columns:
        if column not in stored.columns:
            continue
        for row_position, index in enumerate(stored.index):
            value = stored.at[index, column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if not isinstance(value, SharedArray):
                if column in path_columns:
                    continue
                raise CacheCorruptionError(
                    f"Shared-array output column contains unsupported value: {type(value).__name__}"
                )
            record_relative, entry, path = _write_shared_array_asset(
                value,
                column=column,
                row_index=index,
                row_position=row_position,
                staging_assets_dir=staging_assets_dir,
            )
            if record_relative in owned_assets:
                raise CacheCorruptionError(
                    f"Duplicate shared-array asset path: {record_relative}"
                )
            owned_assets[record_relative] = path
            outputs.append(entry)
            seen_outputs.add(("owned_asset", record_relative))
            stored.at[index, column] = record_relative
    for column in path_columns:
        if column not in stored.columns:
            continue
        for index in stored.index:
            value = stored.at[index, column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if (
                column in shared_array_columns
                and isinstance(value, str)
                and value.startswith("assets/shm/")
            ):
                continue
            if not isinstance(value, (str, os.PathLike)):
                raise CacheCorruptionError(
                    f"Declared path output column {column!r} contains unsupported value: {type(value).__name__}"
                )
            path = Path(value)
            if not path.is_absolute():
                path = Path.cwd() / path
            try:
                path.resolve().relative_to(staging_root)
            except ValueError:
                if column in owned_path_columns:
                    raise CacheCorruptionError(
                        f"Declared owned output asset is outside staging assets: {path}"
                    )
                external = path.as_posix()
                entry_key = ("external_path", external)
                if entry_key not in seen_outputs:
                    outputs.append(
                        {"path": external, "kind": "external_path", "identity": "path"}
                    )
                    seen_outputs.add(entry_key)
                stored.at[index, column] = external
                continue
            record_relative = _add_processing_owned_asset(
                path=path,
                staging_root=staging_root,
                outputs=outputs,
                owned_assets=owned_assets,
                seen_outputs=seen_outputs,
            )
            assert record_relative is not None
            stored.at[index, column] = record_relative
    for column, row_index, value in declared_owned_artifact_paths or ():
        if column not in path_columns:
            continue
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        if not isinstance(value, (str, os.PathLike)):
            raise CacheCorruptionError(
                f"Declared owned output path {column!r} contains unsupported value: {type(value).__name__}"
            )
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        _add_processing_owned_asset(
            path=path,
            staging_root=staging_root,
            outputs=outputs,
            owned_assets=owned_assets,
            seen_outputs=seen_outputs,
            output_column=str(column),
            row_index=row_index,
            require_exists=False,
        )
    for column, row_index, value in declared_scalar_outputs or ():
        _add_processing_scalar_output(
            output_column=str(column),
            row_index=row_index,
            value=value,
            outputs=outputs,
            seen_outputs=seen_scalar_outputs,
        )
    return stored, outputs, owned_assets
