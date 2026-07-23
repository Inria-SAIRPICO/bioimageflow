"""Safe shared-filesystem materialization for archive worker origins."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any

from bioimageflow_core.worker_origins import (
    ArchiveModuleOriginV1,
    decode_worker_tool_origin,
    encode_worker_tool_origin,
)


_SINGLE_FIELDS = frozenset(
    {"id", "module", "filename", "source_hash", "source"}
)
_BUNDLE_FIELDS = frozenset(
    {
        "id",
        "module",
        "filename",
        "root_package",
        "source_hash",
        "files",
    }
)
_FILE_FIELDS = frozenset({"path", "encoding", "content", "source_hash"})


class ParslMaterializationError(ValueError):
    """An archive source cannot be installed as a verified immutable tree."""


@dataclass(frozen=True, slots=True)
class MaterializedArchiveSource:
    """A verified archive origin rewritten to its shared immutable root."""

    origin: ArchiveModuleOriginV1
    directory: Path
    reused: bool


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ParslMaterializationError(
            f"Malformed {label}; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}."
        )


def _require_text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParslMaterializationError(
            f"{field} must be a non-empty, trimmed string."
        )
    if "\x00" in value:
        raise ParslMaterializationError(f"{field} contains a null byte.")
    return value


def _require_hash(value: Any, *, field: str) -> str:
    text = _require_text(value, field=field)
    if (
        len(text) != 64
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise ParslMaterializationError(
            f"{field} must be a lowercase SHA-256 digest."
        )
    return text


def _safe_relative_path(value: Any, *, root_package: str) -> PurePosixPath:
    text = _require_text(value, field="Custom source file path")
    if "\\" in text:
        raise ParslMaterializationError(
            f"Custom source file path must use POSIX separators: {text!r}."
        )
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != root_package
    ):
        raise ParslMaterializationError(
            f"Custom source file path escapes its root package: {text!r}."
        )
    return path


def _decoded_file(
    file_record: Any,
    *,
    root_package: str,
) -> tuple[PurePosixPath, bytes, str]:
    if type(file_record) is not dict:
        raise ParslMaterializationError(
            "Custom source file records must be plain objects."
        )
    _require_exact_fields(
        file_record,
        _FILE_FIELDS,
        label="custom source file record",
    )
    path = _safe_relative_path(
        file_record["path"],
        root_package=root_package,
    )
    if file_record["encoding"] != "base64":
        raise ParslMaterializationError(
            "Custom source file encoding must be 'base64'."
        )
    content = file_record["content"]
    if type(content) is not str:
        raise ParslMaterializationError(
            "Custom source file content must be a base64 string."
        )
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ParslMaterializationError(
            f"Custom source file {path.as_posix()!r} has invalid base64 content."
        ) from exc
    expected_hash = _require_hash(
        file_record["source_hash"],
        field=f"Custom source file {path.as_posix()!r} hash",
    )
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise ParslMaterializationError(
            f"Custom source file {path.as_posix()!r} hash mismatch."
        )
    return path, data, actual_hash


def _bundle_files(
    record: Mapping[str, Any],
) -> tuple[tuple[PurePosixPath, bytes, str], ...]:
    root_package = _require_text(
        record["root_package"],
        field="Custom source root_package",
    )
    files = record["files"]
    if type(files) is not list or not files:
        raise ParslMaterializationError(
            "Custom source files must be a non-empty array."
        )
    decoded = tuple(
        _decoded_file(file_record, root_package=root_package)
        for file_record in files
    )
    paths = [path.as_posix() for path, _data, _digest in decoded]
    duplicate_paths = sorted(
        {path for path in paths if paths.count(path) > 1}
    )
    if duplicate_paths:
        raise ParslMaterializationError(
            f"Custom source bundle contains duplicate paths: {duplicate_paths}."
        )
    return tuple(sorted(decoded, key=lambda item: item[0].as_posix()))


def _bundle_hash(
    files: Iterable[tuple[PurePosixPath, bytes, str]],
) -> str:
    digest = hashlib.sha256()
    for path, _data, file_hash in files:
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_record(
    origin: ArchiveModuleOriginV1,
    record: Mapping[str, Any],
) -> tuple[str | None, tuple[tuple[PurePosixPath, bytes, str], ...]]:
    expected_fields = _BUNDLE_FIELDS if "files" in record else _SINGLE_FIELDS
    _require_exact_fields(record, expected_fields, label="custom source record")
    source_id = _require_text(record["id"], field="Custom source id")
    source_hash = _require_hash(
        record["source_hash"],
        field="Custom source hash",
    )
    expected_source_id = f"m_{source_hash[:16]}"
    if source_id != expected_source_id:
        raise ParslMaterializationError(
            f"Custom source id {source_id!r} does not match its content hash; "
            f"expected {expected_source_id!r}."
        )
    canonical_module = _require_text(
        record["module"],
        field="Custom source module",
    )
    filename = _require_text(record["filename"], field="Custom source filename")
    filename_path = PurePosixPath(filename)
    if (
        filename_path.is_absolute()
        or len(filename_path.parts) != 1
        or filename_path.name != filename
    ):
        raise ParslMaterializationError(
            "Custom source filename must be one safe relative filename."
        )
    if source_id != origin.source_id:
        raise ParslMaterializationError(
            f"Custom source id {source_id!r} does not match origin "
            f"{origin.source_id!r}."
        )
    if source_hash != origin.source_hash:
        raise ParslMaterializationError(
            f"Custom source hash {source_hash!r} does not match origin "
            f"{origin.source_hash!r}."
        )
    if canonical_module != origin.canonical_module:
        raise ParslMaterializationError(
            f"Custom source module {canonical_module!r} does not match origin "
            f"{origin.canonical_module!r}."
        )

    if "files" in record:
        files = _bundle_files(record)
        if _bundle_hash(files) != source_hash:
            raise ParslMaterializationError(
                f"Custom source bundle {source_id!r} hash mismatch."
            )
        scoped_root = origin.scoped_module.split(".", 1)[0]
        expected_scoped = f"{scoped_root}.{canonical_module}"
        if origin.scoped_module != expected_scoped:
            raise ParslMaterializationError(
                f"Archive scoped module {origin.scoped_module!r} does not "
                f"preserve canonical module {canonical_module!r}."
            )
        return None, files

    source = record["source"]
    if type(source) is not str:
        raise ParslMaterializationError(
            "Single-file custom source must contain text."
        )
    actual_hash = hashlib.sha256(source.encode()).hexdigest()
    if actual_hash != source_hash:
        raise ParslMaterializationError(
            f"Custom source module {source_id!r} hash mismatch."
        )
    if "." in origin.scoped_module:
        raise ParslMaterializationError(
            "Single-file archive sources require a top-level scoped module."
        )
    return source, ()


def _write_staging_tree(
    staging: Path,
    origin: ArchiveModuleOriginV1,
    source: str | None,
    files: tuple[tuple[PurePosixPath, bytes, str], ...],
) -> None:
    if source is not None:
        (staging / f"{origin.scoped_module}.py").write_text(
            source,
            encoding="utf-8",
        )
        return
    package_root = staging / origin.scoped_module.split(".", 1)[0]
    package_root.mkdir()
    (package_root / "__init__.py").write_bytes(b"")
    for relative, data, _file_hash in files:
        destination = package_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def _tree_hash(directory: Path, origin: ArchiveModuleOriginV1) -> str:
    entries = list(directory.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ParslMaterializationError(
                f"Materialized archive contains a symlink: {path}."
            )
        if not path.is_file() and not path.is_dir():
            raise ParslMaterializationError(
                f"Materialized archive contains a special file: {path}."
            )
    scoped_root = origin.scoped_module.split(".", 1)[0]
    package_root = directory / scoped_root
    if not package_root.is_dir():
        module_file = directory.joinpath(
            *origin.scoped_module.split(".")
        ).with_suffix(".py")
        if not module_file.is_file():
            raise ParslMaterializationError(
                f"Materialized archive module {origin.scoped_module!r} is absent."
            )
        extra = [
            path for path in entries if path.is_file() and path != module_file
        ]
        if extra:
            raise ParslMaterializationError(
                f"Single-file archive contains unexpected files: {extra}."
            )
        return hashlib.sha256(module_file.read_bytes()).hexdigest()

    files = [
        path
        for path in entries
        if path.is_file()
        and not (
            path.parent == package_root
            and path.name == "__init__.py"
            and path.stat().st_size == 0
        )
    ]
    digest = hashlib.sha256()
    for path in sorted(
        files,
        key=lambda item: item.relative_to(package_root).as_posix(),
    ):
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_materialized(
    directory: Path,
    origin: ArchiveModuleOriginV1,
) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ParslMaterializationError(
            f"Archive destination is not a real directory: {directory}."
        )
    if _tree_hash(directory, origin) != origin.source_hash:
        raise ParslMaterializationError(
            f"Existing archive destination {directory} has mismatched content."
        )


def _make_read_only(directory: Path) -> None:
    files = [path for path in directory.rglob("*") if path.is_file()]
    directories = [path for path in directory.rglob("*") if path.is_dir()]
    for path in files:
        path.chmod(0o444)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)
    directory.chmod(0o555)


def _shared_root(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError("shared_runtime_root must be a string or Path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ParslMaterializationError(
            "shared_runtime_root must be an absolute path."
        )
    return path.resolve(strict=False)


def materialize_archive_source(
    origin: ArchiveModuleOriginV1,
    source_record: Mapping[str, Any],
    *,
    shared_runtime_root: str | Path,
) -> MaterializedArchiveSource:
    """Validate, atomically install, and rewrite one archive worker origin."""
    if type(origin) is not ArchiveModuleOriginV1:
        raise TypeError("origin must be an ArchiveModuleOriginV1.")
    canonical_origin = decode_worker_tool_origin(
        encode_worker_tool_origin(origin)
    )
    assert isinstance(canonical_origin, ArchiveModuleOriginV1)
    if type(source_record) is not dict:
        raise TypeError("source_record must be a plain dictionary.")
    source, files = _validate_record(canonical_origin, source_record)

    runtime_root = _shared_root(shared_runtime_root)
    namespace = runtime_root / "archive_sources"
    namespace.mkdir(parents=True, exist_ok=True)
    if namespace.is_symlink():
        raise ParslMaterializationError(
            f"Archive namespace must not be a symlink: {namespace}."
        )
    try:
        namespace.resolve(strict=True).relative_to(runtime_root)
    except (OSError, ValueError) as exc:
        raise ParslMaterializationError(
            f"Archive namespace escapes shared_runtime_root: {namespace}."
        ) from exc
    destination = namespace / canonical_origin.source_hash
    shared_origin = replace(
        canonical_origin,
        materialization_root=str(destination),
    )
    if destination.exists() or destination.is_symlink():
        _validate_materialized(destination, shared_origin)
        return MaterializedArchiveSource(
            origin=shared_origin,
            directory=destination,
            reused=True,
        )

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{canonical_origin.source_hash}.staging-",
            dir=namespace,
        )
    )
    reused = False
    try:
        _write_staging_tree(staging, shared_origin, source, files)
        _validate_materialized(staging, shared_origin)
        try:
            os.rename(staging, destination)
        except OSError:
            if not destination.exists() and not destination.is_symlink():
                raise
            _validate_materialized(destination, shared_origin)
            reused = True
        else:
            _make_read_only(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return MaterializedArchiveSource(
        origin=shared_origin,
        directory=destination,
        reused=reused,
    )


def materialize_archive_tool_source(
    source_record: Mapping[str, Any],
    *,
    class_name: str,
    shared_runtime_root: str | Path,
) -> MaterializedArchiveSource:
    """Construct and materialize an archive origin directly from its record."""
    origin = archive_origin_from_source_record(
        source_record,
        class_name=class_name,
        shared_runtime_root=shared_runtime_root,
    )
    return materialize_archive_source(
        origin,
        source_record,
        shared_runtime_root=shared_runtime_root,
    )


def archive_origin_from_source_record(
    source_record: Mapping[str, Any],
    *,
    class_name: str,
    shared_runtime_root: str | Path,
) -> ArchiveModuleOriginV1:
    """Build the deterministic archive origin used during static routing."""
    if type(source_record) is not dict:
        raise TypeError("source_record must be a plain dictionary.")
    source_id = _require_text(source_record.get("id"), field="Custom source id")
    source_hash = _require_hash(
        source_record.get("source_hash"),
        field="Custom source hash",
    )
    canonical_module = _require_text(
        source_record.get("module"),
        field="Custom source module",
    )
    scoped_root = f"bioimageflow_custom_tools_{source_id}"
    scoped_module = (
        f"{scoped_root}.{canonical_module}"
        if "files" in source_record
        else scoped_root
    )
    runtime_root = _shared_root(shared_runtime_root)
    destination = runtime_root / "archive_sources" / source_hash
    origin = ArchiveModuleOriginV1(
        source_id=source_id,
        source_hash=source_hash,
        canonical_module=canonical_module,
        scoped_module=scoped_module,
        materialization_root=str(destination),
        class_name=class_name,
    )
    canonical_origin = decode_worker_tool_origin(
        encode_worker_tool_origin(origin)
    )
    assert isinstance(canonical_origin, ArchiveModuleOriginV1)
    _validate_record(canonical_origin, source_record)
    return canonical_origin


def source_record_by_id(
    source_records: Iterable[Mapping[str, Any]],
    source_id: str,
) -> Mapping[str, Any]:
    """Select exactly one archive source record by its canonical ID."""
    matches = [
        record
        for record in source_records
        if isinstance(record, Mapping) and record.get("id") == source_id
    ]
    if len(matches) != 1:
        raise ParslMaterializationError(
            f"Expected exactly one custom source record for {source_id!r}; "
            f"found {len(matches)}."
        )
    return matches[0]


__all__ = [
    "MaterializedArchiveSource",
    "ParslMaterializationError",
    "archive_origin_from_source_record",
    "materialize_archive_source",
    "materialize_archive_tool_source",
    "source_record_by_id",
]
