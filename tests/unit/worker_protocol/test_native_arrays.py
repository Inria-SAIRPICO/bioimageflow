"""Native numeric ownership keeps data immutable and ndarray metadata local."""

import numpy as np
import pytest

from bioimageflow_core import SharedMemoryContext, accept_native_array
from bioimageflow_core._processing_values import decode_processing_value, encode_processing_value


@pytest.mark.parametrize("producer", [
    np.array(4, dtype=np.uint64),
    np.empty((0, 3), dtype=np.float32),
    np.arange(12, dtype=np.int64).reshape(3, 4)[:, ::2],
    np.array([1, 2], dtype=">u8"),
    np.array([np.nan, np.inf, -0.0]),
    np.array([(2, 3.5)], dtype=[("label", ">u8"), ("value", "<f8")]),
])
def test_native_acceptance_preserves_values_dtype_shape_and_isolates_producer(producer):
    original = producer.copy()
    accepted = accept_native_array(producer)
    assert type(accepted) is np.ndarray
    assert accepted.dtype == original.dtype and accepted.shape == original.shape
    assert accepted.flags.c_contiguous and not accepted.flags.writeable
    np.testing.assert_array_equal(accepted, original)
    producer[...] = np.zeros_like(producer)
    np.testing.assert_array_equal(accepted, original)
    with pytest.raises(ValueError):
        accepted.setflags(write=True)
    # Current typed ndarray wire preserves structured fields as well as scalars.
    decoded = decode_processing_value(encode_processing_value(accepted))
    assert type(decoded) is np.ndarray and decoded.dtype == original.dtype
    assert decoded.shape == original.shape
    np.testing.assert_array_equal(decoded, original)


def test_native_reacceptance_shares_only_immutable_data_not_array_metadata():
    accepted = accept_native_array(np.array([4, 7], dtype=np.uint64))
    borrowed = accept_native_array(accepted)
    assert borrowed is not accepted and np.shares_memory(borrowed, accepted)
    borrowed.shape = (2, 1)
    borrowed.dtype = np.uint8
    assert accepted.shape == (2,) and accepted.dtype == np.uint64
    np.testing.assert_array_equal(accepted, [4, 7])


@pytest.mark.parametrize("readonly_kind", ["owning", "mutable_memoryview"])
def test_readonly_flag_is_not_immutable_backing(readonly_kind):
    if readonly_kind == "owning":
        producer = np.array([4], dtype=np.uint8)
        producer.setflags(write=False)
        def mutate():
            producer.setflags(write=True)
            producer.fill(99)
    else:
        backing = bytearray([4])
        producer = np.frombuffer(memoryview(backing).toreadonly(), dtype=np.uint8)
        def mutate():
            backing[0] = 99
    accepted = accept_native_array(producer)
    mutate()
    np.testing.assert_array_equal(accepted, [4])
    assert not np.shares_memory(accepted, producer)


@pytest.mark.parametrize("dtype", [
    np.dtype(object),
    np.dtype("i4", metadata={"units": "pixels"}),
    np.dtype([("label", np.dtype("i4", metadata={"units": "pixels"}))]),
])
def test_native_acceptance_refuses_object_and_nested_dtype_metadata(dtype):
    with pytest.raises(ValueError, match="objects|metadata"):
        accept_native_array(np.zeros(1, dtype=dtype))


def test_whole_native_publication_deduplicates_data_but_not_descriptors(tmp_path):
    owner = SharedMemoryContext(tmp_path)
    producer = np.array([4], dtype=np.uint64)
    published = owner.publish_value({"first": producer, "other": [producer]})
    first, second = published["first"], published["other"][0]
    assert first is not second and np.shares_memory(first, second)
    producer.fill(99)
    first.shape = (1, 1)
    assert second.shape == (1,)
    np.testing.assert_array_equal(second, [4])
    assert owner.close().state == "closed"


def test_worker_accepts_each_native_row_before_next_callback_mutates_producer(tmp_path):
    import hashlib
    from bioimageflow_core import (
        IOModel, ProcessingTask, RowInvocation, SourceFileOriginV1,
        describe_tool_declaration, encode_processing_task, decode_processing_result,
        validate_processing_result,
    )
    from bioimageflow_core.worker import execute_processing_task

    class Controller:
        class Inputs(IOModel):
            value: int
            length: int
        class Outputs(IOModel):
            value: np.ndarray

    source = tmp_path / "reused_native.py"
    source.write_text('''import numpy as np
from bioimageflow_core import ProcessingTool, GENERAL_ENV, RowConsumption, IOModel
LAST = None
class ReusedNative(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: int
        length: int
    class Outputs(IOModel):
        value: np.ndarray
    def process_row(self, arguments):
        global LAST
        if LAST is not None:
            LAST.fill(99)
        LAST = np.full(arguments.length, arguments.value, dtype=np.uint16)
        return self.Outputs(value=LAST)
''')
    task = ProcessingTask(task_id="task_0000000000000001", node_name="native-row-capture",
        invocation_id="inv_" + "1" * 32, cache_attempt_id=None, task_retry=0,
        mode="row_chunk", row_consumption="mapped", declaration=describe_tool_declaration(Controller()),
        tool=SourceFileOriginV1(path=str(source.resolve()), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(), class_name="ReusedNative"),
        rows=tuple(RowInvocation(i, f"actual-{i}", {"value": value, "length": length}, None)
                   for i, (value, length) in enumerate([(4, 1), (7, 2), (9, 1)])))
    result = decode_processing_result(execute_processing_task(encode_processing_task(task)))
    validate_processing_result(task, result)
    assert [group.outputs[0]["value"].tolist() for group in result.groups] == [[4], [7, 7], [9]]
    for row, group in zip(task.rows, result.groups):
        array = group.outputs[0]["value"]
        assert type(array) is np.ndarray and array.dtype == np.uint16
        assert array.shape == (row.arguments["length"],)
        assert [(item.position, item.row_index) for item in group.consumed_rows] == [(row.position, row.row_index)]


def test_worker_accepts_each_shared_row_before_next_callback_mutates_producer(tmp_path):
    import hashlib
    from bioimageflow_core import (
        IOModel, ProcessingTask, RowInvocation, SourceFileOriginV1, SharedArray,
        describe_tool_declaration, encode_processing_task, decode_processing_result,
        validate_processing_result,
    )
    from bioimageflow_core.worker import execute_processing_task

    class Controller:
        class Inputs(IOModel):
            value: int
        class Outputs(IOModel):
            value: SharedArray

    source = tmp_path / "reused_shared.py"
    source.write_text('''import numpy as np
from bioimageflow_core import ProcessingTool, GENERAL_ENV, RowConsumption, IOModel, SharedArray
from bioimageflow_core.shm import create_shared_output, open_shared_array
LAST = None
class ReusedShared(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: int
    class Outputs(IOModel):
        value: SharedArray
    def process_row(self, arguments):
        global LAST
        if LAST is not None:
            with open_shared_array(LAST, writable=True) as previous:
                previous.fill(99)
        with create_shared_output(np.array([arguments.value], dtype=np.uint16)) as producer:
            LAST = producer
        return self.Outputs(value=producer)
''')
    owner = SharedMemoryContext(tmp_path / "scope")
    scope = owner.task_scope("row-callback")
    task = ProcessingTask(task_id="task_0000000000000002", node_name="shared-row-capture",
        invocation_id="inv_" + "2" * 32, cache_attempt_id=None, task_retry=0,
        mode="row_chunk", row_consumption="mapped", declaration=describe_tool_declaration(Controller()),
        tool=SourceFileOriginV1(path=str(source.resolve()), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(), class_name="ReusedShared"),
        rows=tuple(RowInvocation(i, f"actual-{i}", {"value": value}, None)
                   for i, value in enumerate([4, 7, 9])),
        shared_memory_context={"output": scope.descriptor(), "inputs": []})
    try:
        result = decode_processing_result(execute_processing_task(encode_processing_task(task)))
        validate_processing_result(task, result)
        outputs = scope.accept_result([group.outputs for group in result.groups])
        values = []
        for group in outputs:
            array = scope.open(group[0]["value"])
            values.append(array.tolist())
            del array
        assert values == [[4], [7], [9]]
    finally:
        assert owner.close().state == "closed"


def test_worker_publication_budget_failure_keeps_unreturned_backing_until_grant_drain(tmp_path):
    import hashlib
    from bioimageflow_core import (
        IOModel, ProcessingTask, RowInvocation, SourceFileOriginV1, SharedArray,
        describe_tool_declaration, encode_processing_task,
    )
    from bioimageflow_core.worker import execute_processing_task

    class Controller:
        class Inputs(IOModel):
            pass
        class Outputs(IOModel):
            value: SharedArray

    source = tmp_path / "budgeted_output.py"
    source.write_text('''import numpy as np
from bioimageflow_core import ProcessingTool, GENERAL_ENV, RowConsumption, IOModel, SharedArray
from bioimageflow_core.shm import create_shared_output
class BudgetedOutput(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        pass
    class Outputs(IOModel):
        value: SharedArray
    def process_row(self, arguments):
        with create_shared_output(np.array([4], dtype=np.uint16)) as producer:
            return self.Outputs(value=producer)
''')
    owner = SharedMemoryContext(tmp_path / "scopes", max_bytes=200)
    scope = owner.task_scope("budgeted-callback")
    grant = scope.acquire_worker_grant()
    task = ProcessingTask(task_id="task_0000000000000003", node_name="budgeted-row",
        invocation_id="inv_" + "3" * 32, cache_attempt_id=None, task_retry=0,
        mode="row_chunk", row_consumption="mapped", declaration=describe_tool_declaration(Controller()),
        tool=SourceFileOriginV1(path=str(source.resolve()), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(), class_name="BudgetedOutput"),
        rows=(RowInvocation(0, "actual-row", {}, None),),
        shared_memory_context={"output": scope.descriptor(), "inputs": []})
    with pytest.raises(ValueError, match="captured byte budget"):
        execute_processing_task(encode_processing_task(task))
    scope.discard_unreturned()
    pending = owner.close()
    assert pending.state == "pending" and pending.pending_grants == 1 and pending.pending_files == 1
    assert len(list(tmp_path.rglob("*.npy"))) == 1
    grant.drained()
    assert owner.status().state == "closed" and not list(tmp_path.rglob("*.npy"))
