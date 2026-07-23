"""Exact worker protocol and origin codec tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from bioimageflow_core import (
    ArchiveModuleOriginV1,
    InstalledModuleOriginV1,
    ProcessingTaskResultV1,
    ProcessingTaskV1,
    RowInvocationV1,
    RowResultV1,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    VersionedModuleOriginV1,
    decode_processing_result,
    decode_processing_task,
    decode_worker_tool_origin,
    encode_processing_result,
    encode_processing_task,
    encode_worker_tool_origin,
    validate_processing_result,
    worker_tool_origin_identity,
)


def _source_origin(tmp_path) -> SourceFileOriginV1:
    source = tmp_path / "tool.py"
    source.write_text("# worker tool\n", encoding="utf-8")
    return SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash="a" * 64,
        class_name="ExampleTool",
    )


def _context(tmp_path, *, row: bool) -> dict[str, str | None]:
    run_dir = tmp_path.resolve() / "run"
    return {
        "run_dir": str(run_dir),
        "assets_dir": str(run_dir / "assets"),
        "work_dir": str(run_dir / "work"),
        "rows_dir": str(run_dir / "work" / "rows"),
        "row_dir": str(run_dir / "work" / "rows" / "000000") if row else None,
        "batch_dir": None if row else str(run_dir / "work" / "batch"),
        "row_index": "sample" if row else None,
    }


def _task(tmp_path) -> ProcessingTaskV1:
    return ProcessingTaskV1(
        task_id="task_0000000000000000",
        node_name="nested/tool",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=f"att_{'2' * 32}",
        task_retry=0,
        mode="row_chunk",
        tool=_source_origin(tmp_path),
        rows=(
            RowInvocationV1(
                position=0,
                row_index="sample",
                arguments={"value": 3},
                context=_context(tmp_path, row=True),
            ),
        ),
    )


def _result(task: ProcessingTaskV1) -> ProcessingTaskResultV1:
    return ProcessingTaskResultV1(
        task_id=task.task_id,
        node_name=task.node_name,
        invocation_id=task.invocation_id,
        cache_attempt_id=task.cache_attempt_id,
        task_retry=task.task_retry,
        mode=task.mode,
        rows=(
            RowResultV1(
                position=0,
                row_index="sample",
                outputs=({"value": 4},),
            ),
        ),
        metrics={"worker_seconds": 0.25},
    )


def test_processing_task_has_exact_round_trip(tmp_path) -> None:
    task = _task(tmp_path)
    assert decode_processing_task(encode_processing_task(task)) == task


def test_processing_result_has_exact_round_trip(tmp_path) -> None:
    result = _result(_task(tmp_path))
    assert decode_processing_result(encode_processing_result(result)) == result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema="bioimageflow.processing_task.v2"),
        lambda payload: payload.update(mode="future"),
        lambda payload: payload.update(task_id="task_1"),
        lambda payload: payload.update(invocation_id="run_" + "1" * 32),
        lambda payload: payload.update(cache_attempt_id="attempt"),
        lambda payload: payload.update(task_retry=True),
        lambda payload: payload.update(task_retry=1),
        lambda payload: payload.update(extra=True),
        lambda payload: payload.pop("node_name"),
        lambda payload: payload["rows"].append(dict(payload["rows"][0])),
        lambda payload: payload["rows"][0].update(position=True),
        lambda payload: payload["rows"][0].update(extra=True),
    ],
)
def test_processing_task_malformed_payloads_fail_closed(tmp_path, mutate) -> None:
    payload = encode_processing_task(_task(tmp_path))
    mutate(payload)
    with pytest.raises(ValueError):
        decode_processing_task(payload)


def test_batch_requires_batch_context(tmp_path) -> None:
    payload = encode_processing_task(_task(tmp_path))
    payload["mode"] = "process_batch"
    with pytest.raises(ValueError, match="require batch_context"):
        decode_processing_task(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema="bioimageflow.processing_result.v2"),
        lambda payload: payload.update(mode="future"),
        lambda payload: payload.update(task_retry=True),
        lambda payload: payload.update(extra=True),
        lambda payload: payload.pop("rows"),
        lambda payload: payload["rows"].append(dict(payload["rows"][0])),
        lambda payload: payload["rows"][0].update(position=True),
        lambda payload: payload["rows"][0].update(outputs=[3]),
    ],
)
def test_processing_result_malformed_payloads_fail_closed(tmp_path, mutate) -> None:
    payload = encode_processing_result(_result(_task(tmp_path)))
    mutate(payload)
    with pytest.raises(ValueError):
        decode_processing_result(payload)


def test_result_correlation_must_match_exactly(tmp_path) -> None:
    task = _task(tmp_path)
    result = _result(task)
    validate_processing_result(task, result)
    with pytest.raises(ValueError, match="correlation"):
        validate_processing_result(
            task,
            replace(result, invocation_id=f"inv_{'3' * 32}"),
        )
    with pytest.raises(ValueError, match="rows"):
        validate_processing_result(
            task,
            replace(
                result,
                rows=(replace(result.rows[0], row_index="different"),),
            ),
        )


def test_every_origin_variant_has_an_exact_round_trip(tmp_path) -> None:
    root = str(tmp_path.resolve())
    origins = (
        InstalledModuleOriginV1(
            distribution="example-tools",
            version="1.2.3",
            module="example_tools.processing",
            class_name="ExampleTool",
        ),
        VersionedModuleOriginV1(
            distribution="example-tools",
            import_package="example_tools",
            version="1.2.3",
            canonical_module="example_tools.processing",
            scoped_module="example_tools__1_2_3.processing",
            store_root=root,
            class_name="ExampleTool",
        ),
        SharedModuleOriginV1(
            module="shared_tools.processing",
            import_root=root,
            source_hash="a" * 64,
            class_name="ExampleTool",
        ),
        SourceFileOriginV1(
            path=str((tmp_path / "tool.py").resolve()),
            source_hash="b" * 64,
            class_name="ExampleTool",
        ),
        ArchiveModuleOriginV1(
            source_id="m_1234567890abcdef",
            source_hash="c" * 64,
            canonical_module="tools.processing",
            scoped_module="bioimageflow_custom_tools_m_1234567890abcdef.tools.processing",
            materialization_root=root,
            class_name="ExampleTool",
        ),
    )
    for origin in origins:
        payload = encode_worker_tool_origin(origin)
        assert decode_worker_tool_origin(payload) == origin
        assert len(worker_tool_origin_identity(origin)) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "bioimageflow.worker_tool_origin.v2"},
        {"kind": "future"},
        {"source_hash": "ABC"},
        {"class_name": "not-a-class"},
        {"path": "relative.py"},
        {"extra": True},
    ],
)
def test_origin_malformed_payloads_fail_closed(tmp_path, mutation) -> None:
    payload = encode_worker_tool_origin(_source_origin(tmp_path))
    payload.update(mutation)
    with pytest.raises(ValueError):
        decode_worker_tool_origin(payload)


def test_origin_identity_covers_complete_origin(tmp_path) -> None:
    first = _source_origin(tmp_path)
    second = replace(first, class_name="OtherTool")
    assert worker_tool_origin_identity(first) != worker_tool_origin_identity(second)
