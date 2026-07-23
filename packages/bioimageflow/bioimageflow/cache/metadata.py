"""Focused cache operations for metadata."""

from __future__ import annotations

from .common import (
    Any,
    Path,
    hashlib,
    json,
    os,
    pd,
)
def cache_load(cache_path: Path) -> pd.DataFrame:
    """Load the canonical Parquet dataframe from a cache record."""
    if cache_path.name != "dataframe.parquet":
        raise ValueError(
            "Cache dataframes must use the canonical dataframe.parquet path."
        )
    df = pd.read_parquet(cache_path)
    df.index = df.index.astype(str)
    return df


def _prepare_dataframe_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize supported path cells and reject unsupported object values."""
    df_save = df.copy()
    for col in df_save.columns:
        if df_save[col].dtype == object or pd.api.types.is_string_dtype(df_save[col]):
            normalized: list[Any] = []
            for value in df_save[col]:
                if isinstance(value, Path):
                    path = value.expanduser()
                    if not path.is_absolute():
                        path = Path.cwd() / path
                    normalized.append(path.as_posix())
                    continue
                if isinstance(value, (str, int, float, bool, type(None))):
                    normalized.append(value)
                    continue
                try:
                    if bool(pd.isna(value)):
                        normalized.append(value)
                        continue
                except (TypeError, ValueError):
                    pass
                if hasattr(value, "item"):
                    scalar = value.item()
                    if isinstance(scalar, (str, int, float, bool, type(None))):
                        normalized.append(scalar)
                        continue
                raise TypeError(
                    f"Unsupported dataframe value in column {str(col)!r}: "
                    f"{type(value).__name__}"
                )
            df_save[col] = normalized
    return df_save


def _write_canonical_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write the canonical dataframe transport artifact."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    prepared = _prepare_dataframe_for_parquet(df)
    table = pa.Table.from_pandas(prepared, preserve_index=True)
    pq.write_table(
        table,
        path,
        version="2.6",
        data_page_version="1.0",
        compression="zstd",
        compression_level=3,
        use_dictionary=False,
        write_statistics=False,
        write_page_index=False,
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        store_schema=True,
        use_compliant_nested_type=True,
        row_group_size=65_536,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_result_metadata(
    result_dir: Path,
    *,
    kind: str,
    node_name: str,
    sig_hash: str,
    result_key: str,
    attempt_id: str,
) -> None:
    metadata_path = result_dir / "result.json"
    metadata = {
        "schema": "bioimageflow.cache.result.v1",
        "kind": kind,
        "node": node_name,
        "logical_digest": sig_hash,
        "result_key": result_key,
    }
    if not metadata_path.exists():
        tmp_path = result_dir / f".result.{attempt_id}.json.tmp"
        tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        os.replace(tmp_path, metadata_path)


def _write_processing_result_metadata(
    result_dir: Path,
    *,
    node_name: str,
    sig_hash: str,
    result_key: str,
    attempt_id: str,
) -> None:
    _write_result_metadata(
        result_dir,
        kind="processing_tool",
        node_name=node_name,
        sig_hash=sig_hash,
        result_key=result_key,
        attempt_id=attempt_id,
    )


def _write_dataframe_result_metadata(
    result_dir: Path,
    *,
    node_name: str,
    sig_hash: str,
    result_key: str,
    attempt_id: str,
) -> None:
    _write_result_metadata(
        result_dir,
        kind="dataframe_tool",
        node_name=node_name,
        sig_hash=sig_hash,
        result_key=result_key,
        attempt_id=attempt_id,
    )


def _iter_result_metadata(
    storage_path: str | Path,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    results_root = Path(storage_path) / "cache" / "v1" / "results"
    if not results_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for result_dir in results_root.glob("*/*/rk_*"):
        current_path = result_dir / "current.json"
        if not current_path.exists():
            continue
        metadata_path = result_dir / "result.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if (
                metadata.get("schema") == "bioimageflow.cache.result.v1"
                and metadata.get("kind") == kind
            ):
                rows.append(metadata)
                continue
    return rows


def iter_dataframe_result_metadata(
    storage_path: str | Path,
) -> list[dict[str, Any]]:
    """Return selected DataFrameTool result metadata."""
    return _iter_result_metadata(
        storage_path,
        kind="dataframe_tool",
    )


def iter_processing_result_metadata(
    storage_path: str | Path,
) -> list[dict[str, Any]]:
    """Return selected ProcessingTool result metadata."""
    return _iter_result_metadata(
        storage_path,
        kind="processing_tool",
    )
