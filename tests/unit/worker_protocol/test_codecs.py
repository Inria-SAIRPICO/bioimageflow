"""Exact worker protocol and origin codec tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from bioimageflow_core import (
    ArchiveModuleOriginV1,
    InstalledModuleOriginV1,
    ProcessingTaskResult,
    ProcessingTask,
    RowInvocation,
    ConsumedRow,
    OutputGroup,
    ReferenceRow,
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


def _task(tmp_path) -> ProcessingTask:
    return ProcessingTask(
        task_id="task_0000000000000000",
        node_name="nested/tool",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=f"att_{'2' * 32}",
        task_retry=0,
        mode="row_chunk",
        row_consumption="mapped",
        tool=_source_origin(tmp_path),
        rows=(
            RowInvocation(
                position=0,
                row_index="sample",
                arguments={"value": 3},
                context=_context(tmp_path, row=True),
            ),
        ),
    )


def _result(task: ProcessingTask) -> ProcessingTaskResult:
    return ProcessingTaskResult(
        task_id=task.task_id,
        node_name=task.node_name,
        invocation_id=task.invocation_id,
        cache_attempt_id=task.cache_attempt_id,
        task_retry=task.task_retry,
        mode=task.mode,
        row_consumption=task.row_consumption,
        groups=(
            OutputGroup(
                consumed_rows=(ConsumedRow(0, "sample"),),
                outputs=({"value": 4},),
            ),
        ),
        metrics={"worker_seconds": 0.25},
    )


def test_processing_task_has_exact_round_trip(tmp_path) -> None:
    task = _task(tmp_path)
    assert decode_processing_task(encode_processing_task(task)) == task


def test_processing_task_recursively_encodes_paths(tmp_path) -> None:
    task = _task(tmp_path)
    path = tmp_path.resolve() / "input.tif"
    task = replace(
        task,
        rows=(
            replace(
                task.rows[0],
                arguments={
                    "input": path,
                    "nested": [{"mask": path}],
                },
            ),
        ),
    )

    payload = encode_processing_task(task)

    assert decode_processing_task(payload).rows[0].arguments == {
        "input": path,
        "nested": [{"mask": path}],
    }


def test_processing_result_has_exact_round_trip(tmp_path) -> None:
    result = _result(_task(tmp_path))
    assert decode_processing_result(encode_processing_result(result)) == result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema="bioimageflow.processing_task.v4"),
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
        lambda payload: payload.update(schema="bioimageflow.processing_result.v4"),
        lambda payload: payload.update(mode="future"),
        lambda payload: payload.update(task_retry=True),
        lambda payload: payload.update(extra=True),
        lambda payload: payload.pop("groups"),
        lambda payload: payload["groups"].append(dict(payload["groups"][0])),
        lambda payload: payload["groups"][0]["consumed_rows"][0].update(position=True),
        lambda payload: payload["groups"][0].update(outputs=[3]),
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
                groups=(replace(result.groups[0], consumed_rows=(ConsumedRow(0,"different"),)),),
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


@pytest.mark.parametrize("direction", ["task", "result"])
def test_shared_array_descriptor_round_trip_preserves_typed_values(
    tmp_path, monkeypatch, direction
) -> None:
    from multiprocessing import shared_memory
    from pathlib import Path
    from bioimageflow_core import SharedArray

    def refuse_shared_memory(*args, **kwargs):
        pytest.fail("Pure descriptor codec must not allocate or attach shared memory")

    monkeypatch.setattr(shared_memory, "SharedMemory", refuse_shared_memory)
    ref = SharedArray(name="owned_numeric_segment", shape=(2, 3), dtype="uint16", scope_id="a" * 32)
    path = Path(tmp_path) / "numeric.tif"
    literal = {"kind": "shared_array", "name": "literal", "shape": [9], "dtype": "u1"}
    values = {"shared": ref, "nested": [ref, (path, literal)], "bytes": b"\x00data"}
    task = _task(tmp_path)
    if direction == "task":
        task = replace(task, rows=(replace(task.rows[0], arguments=values),))
        decoded = decode_processing_task(encode_processing_task(task))
        actual = decoded.rows[0].arguments
    else:
        result = _result(task)
        result = replace(result, groups=(replace(result.groups[0], outputs=(values,)),))
        decoded = decode_processing_result(encode_processing_result(result))
        validate_processing_result(task, decoded)
        actual = decoded.groups[0].outputs[0]
    assert isinstance(actual["shared"], SharedArray)
    assert actual["shared"] == ref
    assert isinstance(actual["nested"], list)
    assert isinstance(actual["nested"][1], tuple)
    assert isinstance(actual["nested"][1][0], Path)
    assert actual == values


@pytest.mark.parametrize("direction", ["task", "result"])
def test_processing_numeric_values_keep_dtype_precision_and_container_identity(
    tmp_path, direction
):
    import numpy as np

    task = _task(tmp_path)
    values = {
        "array": np.arange(12, dtype="uint16").reshape(3, 4)[:, ::2],
        "unsigned": np.uint64(2**63 + 17),
        "signed": np.int16(-3),
        "complex": np.complex64(1 + 2j),
        "boolean": np.bool_(True),
        "nan": np.float64(np.nan),
        "infinity": np.float32(np.inf),
        "literal": {"kind": "numpy_scalar", "value": [1, 2]},
        "nested": {7: (b"raw", [None, True, 4, 2.5])},
    }
    if direction == "task":
        task = replace(task, rows=(replace(task.rows[0], arguments=values),))
        payload = encode_processing_task(task)
        actual = decode_processing_task(payload).rows[0].arguments
    else:
        result = _result(task)
        result = replace(result, groups=(replace(result.groups[0], outputs=(values,)),))
        payload = encode_processing_result(result)
        actual = decode_processing_result(payload).groups[0].outputs[0]
    assert actual["array"].dtype == values["array"].dtype
    np.testing.assert_array_equal(actual["array"], values["array"])
    assert not np.shares_memory(actual["array"], values["array"])
    for field in ("unsigned", "signed", "complex", "boolean", "nan", "infinity"):
        assert type(actual[field]) is type(values[field])
        assert actual[field].dtype == values[field].dtype
        if field == "nan":
            assert np.isnan(actual[field])
        else:
            assert actual[field] == values[field]
    assert actual["literal"] == values["literal"]
    assert actual["nested"] == values["nested"]


@pytest.mark.parametrize("direction", ["task", "result"])
def test_object_array_is_refused_before_any_transport_send(tmp_path, direction):
    import numpy as np

    task = _task(tmp_path)
    values = {"unsafe": np.array([object()], dtype=object)}
    sent = []

    def send(payload):
        sent.append(payload)
        pytest.fail("Object memory must never reach worker transport")

    with pytest.raises(ValueError, match="objects"):
        if direction == "task":
            task = replace(task, rows=(replace(task.rows[0], arguments=values),))
            send(encode_processing_task(task))
        else:
            result = _result(task)
            result = replace(result, groups=(replace(result.groups[0], outputs=(values,)),))
            send(encode_processing_result(result))
    assert sent == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(kind="unknown"),
        lambda value: value.update(shape=[True]),
        lambda value: value.update(dtype="object"),
        lambda value: value.update(name="\x00invalid"),
        lambda value: value.update(extra=1),
    ],
)
def test_malformed_shared_reference_decode_never_attaches(
    tmp_path, monkeypatch, mutate
):
    from multiprocessing import shared_memory
    from bioimageflow_core import SharedArray

    monkeypatch.setattr(
        shared_memory,
        "SharedMemory",
        lambda *a, **kw: pytest.fail("Decoder attached memory"),
    )
    task = _task(tmp_path)
    task = replace(
        task,
        rows=(
            replace(task.rows[0], arguments={"ref": SharedArray("safe", (2,), "u1", "a" * 32)}),
        ),
    )
    payload = encode_processing_task(task)
    node = payload["rows"][0]["arguments"]["items"][0][1]
    mutate(node)
    with pytest.raises((TypeError, ValueError)):
        decode_processing_task(payload)


@pytest.mark.parametrize("name", ["../outside", "absolute/path", "a" * 97])
def test_shared_reference_path_tokens_are_refused_without_storage_access(tmp_path, name):
    from bioimageflow_core import SharedArray
    task = _task(tmp_path)
    task = replace(task, rows=(replace(task.rows[0], arguments={
        "reference": SharedArray(name, (1,), "u1", "a" * 32),
    }),))
    with pytest.raises(ValueError):
        encode_processing_task(task)


def test_scope_descriptors_roundtrip_without_allocating_or_binding(tmp_path, monkeypatch):
    from bioimageflow_core import SharedMemoryContext
    owner = SharedMemoryContext(tmp_path)
    task = replace(_task(tmp_path), shared_memory_context={"output": owner.descriptor(), "inputs": []})
    payload = encode_processing_task(task)
    def no_io(*args, **kwargs):
        raise AssertionError("Pure decoder consulted storage")
    monkeypatch.setattr("bioimageflow_core._shared_storage.verify", no_io)
    decoded = decode_processing_task(payload)
    assert decoded.shared_memory_context == task.shared_memory_context
    assert list(tmp_path.rglob("*.npy")) == []


@pytest.mark.parametrize("values", [(2, 5), ()])
def test_collective_result_requires_exact_complete_consumption(tmp_path, values):
    task = replace(
        _task(tmp_path), mode="process_batch", row_consumption="collective",
        rows=tuple(RowInvocation(i, f"sample-{i}", {"value": value}, None)
                   for i, value in enumerate(values)),
        batch_context=_context(tmp_path, row=False),
    )
    consumed = tuple(ConsumedRow(row.position, row.row_index) for row in task.rows)
    result = replace(_result(task), groups=(OutputGroup(consumed, ({"sum": sum(values)},)),))
    actual = decode_processing_result(encode_processing_result(result))
    validate_processing_result(task, actual)
    for groups in ((), (OutputGroup(consumed, ()), OutputGroup(consumed, ()))):
        with pytest.raises(ValueError, match="one output group"):
            decode_processing_result(encode_processing_result(replace(result, groups=groups)))
    wrong = (ConsumedRow(99, "foreign"),) if not consumed else consumed[:-1]
    with pytest.raises(ValueError, match="rows"):
        validate_processing_result(task, replace(result, groups=(OutputGroup(wrong, ()),)))


def test_batch_context_values_are_typed_and_separate_from_observations(tmp_path):
    from pathlib import Path
    from bioimageflow_core import SharedArray
    task = replace(_task(tmp_path), mode="process_batch", row_consumption="collective",
                   rows=(), batch_context=_context(tmp_path, row=False),
                   batch_arguments={"output": Path(tmp_path) / "sum.npy", "literal": {"kind": "path"}},
                   reference_rows=(ReferenceRow(0, "reference", {"ref": SharedArray("image", (2,), "u1", "a" * 32)}),))
    decoded = decode_processing_task(encode_processing_task(task))
    assert decoded == task
    assert decoded.rows == ()
    assert isinstance(decoded.reference_rows[0].arguments["ref"], SharedArray)
    assert decoded.reference_rows[0].arguments["ref"]._owner is None
    with pytest.raises(ValueError, match="Collective"):
        decode_processing_task(encode_processing_task(replace(task, mode="row_chunk")))
