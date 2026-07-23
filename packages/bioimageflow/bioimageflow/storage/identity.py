"""Canonical identities, safe paths, and dataframe digests."""

from __future__ import annotations

from .common import (
    Any,
    CACHE_SCHEMA_VERSION,
    Mapping,
    PackageNotFoundError,
    Path,
    RECORD_SCHEMA,
    Sequence,
    _OUTPUT_VIEW_MODES,
    _RECORD_ID_RE,
    _RESERVED_NAMES,
    _RESULT_KEY_RE,
    _SHA256_DIGEST_RE,
    base64,
    cast,
    datetime,
    hashlib,
    json,
    math,
    os,
    pd,
    re,
    timezone,
    unicodedata,
    uuid,
    version,
)
from .models import (
    CacheCorruptionError,
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON bytes for hashing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_output_view_mode(mode: str) -> str:
    value = str(mode)
    if value not in _OUTPUT_VIEW_MODES:
        raise ValueError(
            f"Invalid output_view mode '{value}'. Expected one of {sorted(_OUTPUT_VIEW_MODES)}."
        )
    return value


def _sha256_token(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).digest()
    token = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}_{token}"


def make_result_key(material: Any) -> str:
    """Create a deterministic result key from canonical material."""
    return _sha256_token("rk", {"schema": CACHE_SCHEMA_VERSION, "material": material})


def result_shard_parts(result_key: str) -> tuple[str, str]:
    """Return deterministic nested shard path parts for a result key."""
    if not _RESULT_KEY_RE.fullmatch(result_key):
        raise ValueError(f"Invalid result key: {result_key!r}")
    return result_key[3:5], result_key[5:7]


def _validate_record_id(record_id: str) -> str:
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"Invalid record ID: {record_id!r}")
    return record_id


def _validate_sha256_digest(digest: str, *, label: str) -> str:
    if not _SHA256_DIGEST_RE.fullmatch(digest):
        raise CacheCorruptionError(f"Invalid {label} digest.")
    return digest


def _safe_segment(raw: str) -> str:
    normalized = unicodedata.normalize("NFC", raw).strip()
    if not normalized:
        normalized = "node"
    normalized = normalized.replace("/", "_").replace("\\", "_")
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized)
    normalized = normalized.strip("._-") or "node"
    normalized = normalized[:64]
    if normalized.lower() in _RESERVED_NAMES:
        normalized = f"{normalized}_node"
    return normalized.lower()


def make_node_keys(names: list[str]) -> dict[str, str]:
    """Return storage-safe node keys, disambiguating normalized collisions."""
    used: dict[str, str] = {}
    result: dict[str, str] = {}
    for name in names:
        base = _safe_segment(name)
        key = base
        if key in used:
            digest = hashlib.sha1(
                unicodedata.normalize("NFC", name).encode()
            ).hexdigest()[:8]
            key = f"{base}-{digest}"
            counter = 2
            while key in used:
                key = f"{base}-{digest}-{counter}"
                counter += 1
        used[key] = name
        result[name] = key
    return result


def validate_relative_posix_path(path: str) -> str:
    """Validate and normalize a record-relative POSIX path."""
    if "\x00" in path:
        raise ValueError("Path contains NUL byte.")
    if path == "" or path.startswith("/") or "\\" in path:
        raise ValueError(f"Unsafe relative path: {path!r}")
    parts = path.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"Unsafe relative path: {path!r}")
        if part.lower() in _RESERVED_NAMES:
            raise ValueError(f"Reserved path segment: {part!r}")
    return "/".join(parts)


def _validate_path_segment(value: str, *, label: str) -> str:
    segment = validate_relative_posix_path(value)
    if "/" in segment:
        raise ValueError(f"{label} must be one path segment: {value!r}")
    return segment


def _validate_node_key(value: str) -> str:
    return validate_relative_posix_path(value)


def _atomic_write_json(path: Path, payload: dict[str, Any], *, stem: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{stem}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp_path, path)


def _bioimageflow_version() -> str | None:
    try:
        return version("bioimageflow")
    except PackageNotFoundError:
        return None


def _is_missing(value: Any) -> bool:
    if isinstance(value, (str, bytes, Path)):
        return False
    try:
        missing = pd.isna(value)
    except TypeError:
        return False
    if hasattr(missing, "item"):
        try:
            missing = missing.item()
        except ValueError:
            return False
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _datetime_payload(value: datetime | pd.Timestamp) -> dict[str, str]:
    timestamp = cast(pd.Timestamp, pd.Timestamp(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone.utc)
    else:
        timestamp = timestamp.tz_convert(timezone.utc)
    return {"kind": "datetime", "value": timestamp.isoformat().replace("+00:00", "Z")}


def _cell_payload(value: Any, *, column_kind: str = "scalar", dtype: str = "") -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if _is_missing(value):
        return {"kind": "null", "value": None}
    if isinstance(value, (datetime, pd.Timestamp)):
        return _datetime_payload(value)
    if column_kind == "record_asset":
        return {
            "kind": "record_asset",
            "value": validate_relative_posix_path(Path(value).as_posix()),
        }
    if column_kind == "external_path":
        if str(value) == "":
            raise TypeError("Unsupported dataframe value: empty external path")
        path = Path(value).expanduser() if isinstance(value, Path) else Path(str(value))
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.as_posix()
        return {"kind": "external_path", "value": path}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, int):
        kind = "unsigned_integer" if dtype.startswith("uint") else "signed_integer"
        return {"kind": kind, "value": str(value)}
    if isinstance(value, float):
        if math.isnan(value):
            encoded = "NaN"
        elif math.isinf(value):
            encoded = "Infinity" if value > 0 else "-Infinity"
        else:
            encoded = repr(value)
        return {"kind": "float", "value": encoded}
    if isinstance(value, str):
        return {"kind": "string", "value": unicodedata.normalize("NFC", value)}
    if isinstance(value, Path):
        path = value.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return {"kind": "external_path", "value": path.as_posix()}
    raise TypeError(f"Unsupported dataframe value: {type(value).__name__}")


def canonical_scalar_payload(value: Any) -> dict[str, Any]:
    """Return the canonical payload for scalar manifest metadata."""
    payload = _cell_payload(value)
    if payload["kind"] in {"record_asset", "external_path"}:
        raise TypeError(
            "Scalar output metadata must not encode path or asset references."
        )
    return payload


def _ordered_dataframe_columns(
    df: pd.DataFrame,
    declared_columns: Sequence[str] | None,
) -> dict[str, Any]:
    column_by_name: dict[str, Any] = {}
    for column in df.columns:
        name = str(column)
        if name in column_by_name:
            raise ValueError(
                f"Duplicate dataframe column after string conversion: {name!r}"
            )
        column_by_name[name] = column
    declared = [str(column) for column in declared_columns or ()]
    missing = [column for column in declared if column not in column_by_name]
    if missing:
        raise ValueError(f"Declared dataframe columns are missing: {missing!r}")
    additional = sorted(column for column in column_by_name if column not in declared)
    return {name: column_by_name[name] for name in [*declared, *additional]}


def canonical_dataframe_identity(
    df: pd.DataFrame,
    *,
    declared_columns: Sequence[str] | None = None,
    column_kinds: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Return the canonical logical schema and digest for a dataframe."""
    columns = _ordered_dataframe_columns(df, declared_columns)
    kinds = {str(key): value for key, value in (column_kinds or {}).items()}
    column_schema: list[dict[str, Any]] = []
    for name, original in columns.items():
        series = df[original]
        entry: dict[str, Any] = {
            "name": unicodedata.normalize("NFC", name),
            "dtype": str(series.dtype),
            "kind": kinds.get(name, "scalar"),
        }
        if isinstance(series.dtype, pd.CategoricalDtype):
            entry["categories"] = [
                unicodedata.normalize("NFC", str(value))
                for value in series.cat.categories
            ]
            entry["ordered"] = bool(series.cat.ordered)
        if pd.api.types.is_datetime64_any_dtype(series.dtype):
            entry["timezone"] = str(getattr(series.dtype, "tz", None) or "naive-utc")
        column_schema.append(entry)
    rows: list[dict[str, Any]] = []
    for index, row in df.iterrows():
        rows.append(
            {
                "index": unicodedata.normalize("NFC", str(index)),
                "values": {
                    name: _cell_payload(
                        row[original],
                        column_kind=kinds.get(name, "scalar"),
                        dtype=str(df[original].dtype),
                    )
                    for name, original in columns.items()
                },
            }
        )
    payload = {"columns": column_schema, "rows": rows}
    digest = f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"
    return column_schema, digest


def canonical_dataframe_digest(
    df: pd.DataFrame,
    *,
    declared_columns: Sequence[str] | None = None,
    column_kinds: Mapping[str, str] | None = None,
) -> str:
    """Return the canonical logical digest for a dataframe."""
    _schema, digest = canonical_dataframe_identity(
        df,
        declared_columns=declared_columns,
        column_kinds=column_kinds,
    )
    return digest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def asset_digest_and_size(path: Path) -> tuple[int, str]:
    """Return deterministic size and digest metadata for a file or directory asset."""
    if path.is_symlink():
        raise CacheCorruptionError("Asset must not be a symlink.")
    if path.is_file():
        return path.stat().st_size, _file_sha256(path)
    if not path.is_dir():
        raise CacheCorruptionError(f"Asset is not a regular file or directory: {path}")

    root = path.resolve()
    total_size = 0
    entries: list[dict[str, Any]] = []
    normalized_paths: set[str] = set()
    casefold_paths: set[str] = set()
    for child in path.rglob("*"):
        if child.is_symlink():
            raise CacheCorruptionError(f"Directory asset contains a symlink: {child}")
        try:
            child.resolve().relative_to(root)
        except ValueError as exc:
            raise CacheCorruptionError(
                f"Directory asset escapes its root: {child}"
            ) from exc
        relative = unicodedata.normalize("NFC", child.relative_to(path).as_posix())
        relative = validate_relative_posix_path(relative)
        folded = relative.casefold()
        if relative in normalized_paths or folded in casefold_paths:
            raise CacheCorruptionError(
                f"Directory asset contains colliding paths: {relative}"
            )
        normalized_paths.add(relative)
        casefold_paths.add(folded)
        if child.is_dir():
            entries.append({"type": "directory", "path": relative})
            continue
        if not child.is_file():
            raise CacheCorruptionError(
                f"Directory asset contains an unsupported entry: {child}"
            )
        size = child.stat().st_size
        total_size += size
        entries.append(
            {
                "digest": _file_sha256(child),
                "type": "file",
                "path": relative,
                "size": size,
            }
        )
    entries.sort(key=lambda entry: str(entry["path"]))
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"schema": "bioimageflow.directory_asset.v1", "entries": entries}
        )
    ).hexdigest()
    return total_size, f"sha256:{digest}"


def make_record_id(manifest_material: dict[str, Any]) -> str:
    """Create a record ID from content material, excluding execution metadata."""
    excluded = {
        "record_id",
        "attempt_id",
        "run_id",
        "created_at",
        "selected_at",
        "hostname",
        "pid",
        "duration",
    }
    content: dict[str, Any] = {
        key: value for key, value in manifest_material.items() if key not in excluded
    }
    dataframe = content.get("dataframe")
    if isinstance(dataframe, dict):
        content["dataframe"] = {
            key: value for key, value in dataframe.items() if key != "transport_digest"
        }
    return _sha256_token("rec", {"schema": RECORD_SCHEMA, "content": content})
