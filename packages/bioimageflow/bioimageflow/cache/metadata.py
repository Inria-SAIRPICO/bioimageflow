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
from .identity import (
    dataframe_result_key,
    processing_result_key,
)


def cache_load(cache_path: Path) -> pd.DataFrame:
    """Load a DataFrame from cache.

    Accepts either a ``.parquet`` or ``.csv`` path.
    """
    if cache_path.suffix == ".parquet":
        df = pd.read_parquet(cache_path)
    else:
        # CSV support for lightweight fixtures and manually inspected caches.
        df = pd.read_csv(cache_path, index_col=0, keep_default_na=False)
        # Restore numeric columns where possible
        for col in df.columns:
            if pd.api.types.is_string_dtype(df[col]):
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
    df.index = df.index.astype(str)
    return df


def _prepare_dataframe_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    # Parquet requires Arrow-serializable types — convert Path/SharedArray-like
    # objects to strings while preserving ordinary scalar values.
    df_save = df.copy()
    for col in df_save.columns:
        if df_save[col].dtype == object or pd.api.types.is_string_dtype(df_save[col]):
            df_save[col] = df_save[col].apply(
                lambda v: (
                    str(v)
                    if not isinstance(v, (str, int, float, bool, type(None)))
                    else v
                )
            )
    return df_save


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
    result_key_for: Any,
    known_node_signatures: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    results_root = Path(storage_path) / "cache" / "v1" / "results"
    if not results_root.exists():
        return []
    known_node_signatures = known_node_signatures or {}
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
        result_key = result_dir.name
        for node_name, signatures in known_node_signatures.items():
            for sig_hash in signatures:
                if result_key_for(node_name, sig_hash) == result_key:
                    rows.append(
                        {
                            "schema": "bioimageflow.cache.result.v1",
                            "kind": kind,
                            "node": node_name,
                            "logical_digest": sig_hash,
                            "result_key": result_key,
                        }
                    )
    return rows


def iter_dataframe_result_metadata(
    storage_path: str | Path,
    known_node_signatures: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return DataFrameTool result metadata, inferring metadata-less records when possible."""
    return _iter_result_metadata(
        storage_path,
        kind="dataframe_tool",
        result_key_for=dataframe_result_key,
        known_node_signatures=known_node_signatures,
    )


def iter_processing_result_metadata(
    storage_path: str | Path,
    known_node_signatures: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return ProcessingTool result metadata, inferring metadata-less records when possible."""
    return _iter_result_metadata(
        storage_path,
        kind="processing_tool",
        result_key_for=processing_result_key,
        known_node_signatures=known_node_signatures,
    )
