"""Safe materialization of archive worker origins."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path

import pytest

from bioimageflow.parsl.materialization import (
    ParslMaterializationError,
    archive_origin_from_source_record,
    materialize_archive_source,
    materialize_archive_tool_source,
    source_record_by_id,
)
from bioimageflow_core.worker_origins import (
    ArchiveModuleOriginV1,
    load_worker_tool,
)


TOOL_SOURCE = """\
from bioimageflow_core import IOModel, ProcessingTool

class ArchivedTool(ProcessingTool):
    environment = None
    class Outputs(IOModel):
        value: int
    def process_row(self, arguments):
        return self.Outputs(value=1)
"""


def _single_record(source: str = TOOL_SOURCE) -> dict[str, object]:
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    return {
        "id": f"m_{source_hash[:16]}",
        "module": "custom.worker",
        "filename": "worker.py",
        "source_hash": source_hash,
        "source": source,
    }


def _file_record(path: str, data: bytes) -> dict[str, str]:
    return {
        "path": path,
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "source_hash": hashlib.sha256(data).hexdigest(),
    }


def _bundle_hash(files: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(files, key=lambda item: item["path"]):
        digest.update(record["path"].encode())
        digest.update(b"\0")
        digest.update(record["source_hash"].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _bundle_record() -> dict[str, object]:
    files = [
        _file_record("tools/__init__.py", b""),
        _file_record("tools/helper.py", b"VALUE = 7\n"),
        _file_record(
            "tools/worker.py",
            b"from bioimageflow_core import IOModel, ProcessingTool\n"
            b"from .helper import VALUE\n"
            b"class ArchivedTool(ProcessingTool):\n"
            b"    environment = None\n"
            b"    class Outputs(IOModel):\n"
            b"        value: int\n"
            b"    def process_row(self, arguments):\n"
            b"        return self.Outputs(value=VALUE)\n",
        ),
    ]
    source_hash = _bundle_hash(files)
    return {
        "id": f"m_{source_hash[:16]}",
        "module": "tools.worker",
        "filename": "worker.py",
        "root_package": "tools",
        "source_hash": source_hash,
        "files": files,
    }


def test_materializes_single_source_and_loads_it_from_shared_root(
    tmp_path: Path,
) -> None:
    record = _single_record()

    materialized = materialize_archive_tool_source(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )

    expected = (
        tmp_path
        / "archive_sources"
        / str(record["source_hash"])
    )
    assert materialized.directory == expected
    assert materialized.origin.materialization_root == str(expected)
    assert materialized.origin.scoped_module == (
        f"bioimageflow_custom_tools_{record['id']}"
    )
    assert materialized.reused is False
    assert type(load_worker_tool(materialized.origin)).__name__ == "ArchivedTool"


def test_archive_origin_construction_is_verified_and_side_effect_free(
    tmp_path: Path,
) -> None:
    record = _bundle_record()

    origin = archive_origin_from_source_record(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )

    assert origin.materialization_root == str(
        tmp_path / "archive_sources" / str(record["source_hash"])
    )
    assert not (tmp_path / "archive_sources").exists()


def test_materializes_bundle_with_scoped_helpers_and_loads_tool(
    tmp_path: Path,
) -> None:
    record = _bundle_record()

    materialized = materialize_archive_tool_source(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )

    assert materialized.origin.scoped_module == (
        f"bioimageflow_custom_tools_{record['id']}.tools.worker"
    )
    instance = load_worker_tool(materialized.origin)
    assert instance.process_row(None).value == 7


def test_existing_valid_destination_is_reused_without_rewriting(
    tmp_path: Path,
) -> None:
    record = _single_record()
    first = materialize_archive_tool_source(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )
    module_path = first.directory / f"{first.origin.scoped_module}.py"
    first_mtime = module_path.stat().st_mtime_ns

    second = materialize_archive_tool_source(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )

    assert second.origin == first.origin
    assert second.reused is True
    assert module_path.stat().st_mtime_ns == first_mtime


def test_concurrent_materialization_installs_one_complete_tree(
    tmp_path: Path,
) -> None:
    record = _bundle_record()

    def materialize():
        return materialize_archive_tool_source(
            record,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: materialize(), range(8)))

    assert len({result.directory for result in results}) == 1
    assert type(load_worker_tool(results[0].origin)).__name__ == "ArchivedTool"
    namespace = tmp_path / "archive_sources"
    assert not list(namespace.glob(".*.staging-*"))


def test_existing_mismatched_destination_fails_closed(tmp_path: Path) -> None:
    record = _single_record()
    materialized = materialize_archive_tool_source(
        record,
        class_name="ArchivedTool",
        shared_runtime_root=tmp_path.resolve(),
    )
    module_path = materialized.directory / f"{materialized.origin.scoped_module}.py"
    module_path.chmod(0o644)
    module_path.write_text("tampered = True\n")

    with pytest.raises(ParslMaterializationError, match="mismatched content"):
        materialize_archive_tool_source(
            record,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )


@pytest.mark.parametrize(
    ("mutation", "evidence"),
    [
        ({"id": "other"}, "does not match"),
        ({"source_hash": "0" * 64}, "does not match"),
        ({"module": "other.worker"}, "does not match origin"),
        ({"extra": "field"}, "Malformed"),
    ],
)
def test_record_must_exactly_match_archive_origin(
    tmp_path: Path,
    mutation: dict[str, str],
    evidence: str,
) -> None:
    record = _single_record()
    origin = ArchiveModuleOriginV1(
        source_id=str(record["id"]),
        source_hash=str(record["source_hash"]),
        canonical_module=str(record["module"]),
        scoped_module=f"bioimageflow_custom_tools_{record['id']}",
        materialization_root=str(tmp_path.resolve()),
        class_name="ArchivedTool",
    )
    mutated = {**record, **mutation}

    with pytest.raises(ParslMaterializationError, match=evidence):
        materialize_archive_source(
            origin,
            mutated,
            shared_runtime_root=tmp_path.resolve(),
        )


def test_bundle_rejects_traversal_duplicate_bad_base64_and_file_hash(
    tmp_path: Path,
) -> None:
    record = _bundle_record()
    files = record["files"]
    assert isinstance(files, list)

    traversal = {**record, "files": [{**files[0], "path": "../worker.py"}]}
    with pytest.raises(ParslMaterializationError, match="escapes"):
        materialize_archive_tool_source(
            traversal,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )

    duplicate = {**record, "files": [files[0], files[0]]}
    with pytest.raises(ParslMaterializationError, match="duplicate"):
        materialize_archive_tool_source(
            duplicate,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )

    bad_base64 = {**record, "files": [{**files[0], "content": "***"}]}
    with pytest.raises(ParslMaterializationError, match="base64"):
        materialize_archive_tool_source(
            bad_base64,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )

    bad_hash = {
        **record,
        "files": [{**files[0], "source_hash": "0" * 64}],
    }
    with pytest.raises(ParslMaterializationError, match="file.*hash mismatch"):
        materialize_archive_tool_source(
            bad_hash,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )


def test_record_rejects_unsafe_filename(tmp_path: Path) -> None:
    record = {**_single_record(), "filename": "../worker.py"}

    with pytest.raises(ParslMaterializationError, match="safe relative filename"):
        materialize_archive_tool_source(
            record,
            class_name="ArchivedTool",
            shared_runtime_root=tmp_path.resolve(),
        )


def test_shared_runtime_root_must_be_absolute() -> None:
    with pytest.raises(ParslMaterializationError, match="absolute"):
        materialize_archive_tool_source(
            _single_record(),
            class_name="ArchivedTool",
            shared_runtime_root="relative/runtime",
        )


def test_source_record_selection_requires_exactly_one_match() -> None:
    record = _single_record()

    assert source_record_by_id([record], str(record["id"])) is record
    with pytest.raises(ParslMaterializationError, match="found 0"):
        source_record_by_id([record], "missing")
    with pytest.raises(ParslMaterializationError, match="found 2"):
        source_record_by_id([record, record], str(record["id"]))
