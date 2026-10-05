"""Exact returned-group lifetime through public Workflow results."""
from pathlib import Path
import sys
import weakref

import numpy as np
import pytest

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow_core import SharedMemoryContext
from bioimageflow_core.shm import create_shared_output, open_shared_array
from tests.testkit.runtime_cache import SourceSharedMemoryWriter


def test_discarded_default_workflow_results_release_exact_allocations(tmp_path: Path) -> None:
    """Controlled CPython refcount witness; no GC polling or timing sleeps."""
    assert sys.implementation.name == "cpython"
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        node = SourceSharedMemoryWriter()(value=7)
    owner = workflow.shared_memory_context
    try:
        for _ in range(3):
            frame = workflow.compute(node)
            assert frame.at["0", "result"].shape == (2, 2)
            del frame
            assert owner.status().pending_files == 0
    finally:
        owner.close()


def test_two_run_groups_release_independently_and_keep_existing_view(tmp_path: Path) -> None:
    from bioimageflow import result_groups
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        node = SourceSharedMemoryWriter()(value=7)
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    first = workflow.compute(node, run_context=contexts[0])
    second = workflow.compute(node, run_context=contexts[1])
    a, = result_groups(first)
    b, = result_groups(second)
    assert a is not b and a.group_id != b.group_id
    assert contexts[0].result_groups == (a,)
    assert contexts[1].result_groups == (b,)
    ref_a, ref_b = first.at["0", "result"], second.at["0", "result"]
    with open_shared_array(ref_a) as array:
        view = np.asarray(array)[1:]
    del array
    try:
        assert a.release().state == "pending"
        assert view.tolist() == [[7, 7]]
        with pytest.raises(RuntimeError, match="releas|clos"):
            with open_shared_array(ref_a):
                pass
        with open_shared_array(ref_b) as other:
            assert other.tolist() == [[7, 7], [7, 7]]
        del other
        del view
        assert a.status().state == "closed"
        assert a.release().state == "closed"
        assert b.release().state == "closed"
    finally:
        workflow.shared_memory_context.close()


def test_group_binding_has_no_cycle_and_rejects_foreign_discovery(tmp_path: Path) -> None:
    from dataclasses import replace
    from bioimageflow import result_groups
    from bioimageflow.result_groups import bind_result_group
    owner = SharedMemoryContext(tmp_path)
    try:
        with owner.activate(), create_shared_output(np.array([4])) as producer:
            pass
        accepted = owner.publish(producer)
        first, a = bind_result_group({"image": accepted}, node_name="a", group_id="run-a")
        second, b = bind_result_group({"image": accepted}, node_name="b", group_id="run-b")
        assert a is not None and b is not None
        assert first["image"].bound_owner is accepted.bound_owner
        assert second["image"].bound_owner is accepted.bound_owner
        assert first["image"] == second["image"] == accepted
        a.release()
        with open_shared_array(second["image"]) as array:
            assert array.tolist() == [4]
        del array
        with pytest.raises(ValueError, match="foreign|binding"):
            result_groups(replace(second["image"], _group=a))
        weak_group = weakref.ref(b)
        del b, second
        assert weak_group() is None  # actual CPython reference drainage
        assert result_groups({"scalar": 4}) == ()
        with pytest.raises(ValueError, match="group|accepted"):
            result_groups(producer)
    finally:
        owner.close()


def test_shared_input_content_capture_reuses_unchanged_and_recomputes_changed_bytes(tmp_path: Path) -> None:
    from bioimageflow import result_groups
    from bioimageflow_core import Arguments, GENERAL_ENV, IOModel, ProcessingTool, RowConsumption, SharedArray

    class EchoPixels(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = GENERAL_ENV
        executions = 0

        class Inputs(IOModel):
            image: SharedArray

        class Outputs(IOModel):
            value: int
            echo: SharedArray

        def process_row(self, arguments: Arguments):
            type(self).executions += 1
            with open_shared_array(arguments.image) as pixels:
                assert not pixels.flags.writeable
                value = int(pixels[0])
            return self.Outputs(value=value, echo=arguments.image)

    owner = SharedMemoryContext(tmp_path / "producer")
    with owner.activate(), create_shared_output(np.array([4], dtype="uint16")) as producer:
        pass
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        node = EchoPixels()(image=producer)
    try:
        with open_shared_array(producer) as writable:
            first = workflow.compute(node)
            second = workflow.compute(node)
            assert first["value"].tolist() == second["value"].tolist() == [4]
            assert EchoPixels.executions == 1
            writable[0] = 99
            third = workflow.compute(node)
            assert third["value"].tolist() == [99]
            assert EchoPixels.executions == 2
            with open_shared_array(first.at["0", "echo"]) as accepted:
                assert accepted.tolist() == [4]
                assert not accepted.flags.writeable
            del accepted
        del writable
        for value in (first, second, third):
            for group in result_groups(value):
                group.release()
    finally:
        workflow.shared_memory_context.close()
        owner.close()


def test_failed_group_binding_preserves_preexisting_accepted_allocation(tmp_path: Path) -> None:
    from bioimageflow.result_groups import bind_result_group
    owner = SharedMemoryContext(tmp_path)
    try:
        with owner.activate(), create_shared_output(np.array([4])) as producer:
            pass
        accepted = owner.publish(producer)
        with pytest.raises(ValueError, match="accept|seal|publish"):
            bind_result_group({"accepted": accepted, "unpublished": producer},
                              node_name="bad", group_id="failed")
        with open_shared_array(accepted) as pixels:
            assert pixels.tolist() == [4]
        del pixels
        assert owner.status().pending_leases == 0
    finally:
        owner.close()


def test_dataframe_publication_copies_repeated_producer_once(tmp_path: Path) -> None:
    import pandas as pd
    from bioimageflow.result_groups import map_shared_values
    owner = SharedMemoryContext(tmp_path)
    try:
        producer = owner.create(np.array([4], dtype="uint16"))
        frame = pd.DataFrame(
            {"image": [producer, producer],
             "value": np.array([2**53 + 1, 2**53 + 2], dtype="uint64")},
            index=["a", "b"],
        )
        result = map_shared_values(frame, owner.publish_value)
        first, second = result.at["a", "image"], result.at["b", "image"]
        assert first.name == second.name
        assert first.bound_owner is second.bound_owner is owner
        assert owner.status().pending_files == 2  # producer + one accepted copy
        assert result.index.tolist() == ["a", "b"]
        assert result["value"].dtype == np.dtype("uint64")
        assert result["value"].tolist() == [2**53 + 1, 2**53 + 2]
        assert frame.at["a", "image"] is frame.at["b", "image"] is producer
        assert frame["value"].dtype == np.dtype("uint64")
        with open_shared_array(first) as pixels:
            assert pixels.tolist() == [4]
            assert not pixels.flags.writeable
        del pixels
    finally:
        owner.close()
