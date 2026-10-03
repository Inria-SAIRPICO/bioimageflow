"""
Test shared memory management.

Covers:
- create_shared_output / open_shared_array lifecycle
- SharedArray as output field (crosses serialization boundary)
- load_image dispatch between Path and SharedArray
- Shared memory persistence after context manager exit
- Cache converts SharedArray to file and back
"""


from typing import Any

import pytest

from bioimageflow import Workflow

from tests.testkit.integration_tools import (
    FileLoader,
    StubSharedMemoryConsumer,
    StubSharedMemoryTool,
)


@pytest.fixture(autouse=True)
def owned_shared_arrays(tmp_path):
    """Public helper calls explicitly allocate inside a retained controller scope."""
    from bioimageflow_core import SharedMemoryContext

    owner = SharedMemoryContext(tmp_path / "shared")
    with owner.activate():
        yield owner
    owner.close()


@pytest.mark.shared_memory
class TestSharedMemoryWorkflow:

    def test_shm_producer_then_consumer(self, tmp_workspace):
        """SharedArray flows from producer to consumer tool."""
        load = FileLoader()
        producer = StubSharedMemoryTool()
        consumer = StubSharedMemoryConsumer()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            df = wf.compute(result)

            assert len(df) == 3
            assert "num_labels" in df.columns
            # StubSharedMemoryTool creates zeros → only 1 unique label (0)
            assert all(df["num_labels"] == 1)


@pytest.mark.shared_memory
class TestSharedMemoryHelpers:

    def test_create_shared_output_and_open(self):
        """create_shared_output creates a segment; open_shared_array reads it."""
        import numpy as np
        from bioimageflow_core.shm import create_shared_output, open_shared_array

        data = np.arange(100, dtype=np.float32).reshape(10, 10)

        with create_shared_output(data) as ref:
            assert ref.shape == (10, 10)
            assert ref.dtype == "float32"
            assert ref.name.startswith("bif_")

            # Read the data back via open_shared_array
            with open_shared_array(ref) as arr:
                np.testing.assert_array_equal(arr, data)

            # Data still accessible after create context manager exits
            # (close, not unlink)

        # Lexical exit never requests allocation release.

    def test_shared_array_survives_return_inside_with(self):
        """Returning SharedArray from inside a 'with' block is valid."""
        import numpy as np
        from bioimageflow_core.shm import create_shared_output, open_shared_array

        def produce():
            data = np.ones((5, 5), dtype=np.uint8)
            with create_shared_output(data) as ref:
                return ref  # Safe: data outlives the handle

        ref = produce()
        try:
            with open_shared_array(ref) as arr:
                assert arr.sum() == 25
        finally:
            ref.bound_owner.release(ref)


class TestLoadImageDispatch:

    def test_load_image_with_path(self, tmp_workspace):
        """load_image with a Path delegates to file_reader."""
        from bioimageflow_core.io import load_image

        img_path = tmp_workspace / "data" / "cell_01.tif"

        def reader(p):
            return p.read_text()

        with load_image(img_path, file_reader=reader) as data:
            assert data == "FAKE_IMAGE_cell_01.tif"

    @pytest.mark.shared_memory
    def test_load_image_with_shared_array(self):
        """load_image with a SharedArray attaches to shared memory."""
        import numpy as np
        from bioimageflow_core.io import load_image
        from bioimageflow_core.shm import create_shared_output

        original = np.array([1, 2, 3, 4, 5], dtype=np.int32)

        with create_shared_output(original) as ref:
            try:
                def should_not_be_called(p):
                    raise RuntimeError("Should not call file_reader for SharedArray")

                with load_image(ref, file_reader=should_not_be_called) as arr:
                    np.testing.assert_array_equal(arr, original)
            finally:
                ref.bound_owner.release(ref)


class TestSaveImage:

    def test_save_image_delegates_to_writer(self, tmp_workspace):
        """save_image calls the provided file_writer with Path and data."""
        from bioimageflow_core.io import save_image

        out_path = tmp_workspace / "output.txt"
        save_image(out_path, "PIXEL_DATA", file_writer=lambda p, d: (p.write_text(d), None)[-1])

        assert out_path.exists()
        assert out_path.read_text() == "PIXEL_DATA"


@pytest.mark.shared_memory
class TestSharedMemoryCachePersistence:

    def test_cached_shm_output_restored_from_disk(self, tmp_workspace):
        """
        When caching, SharedArray outputs are serialized to disk.
        On cache hit, they are restored to new SharedArray segments.
        """
        load = FileLoader()
        producer = StubSharedMemoryTool()
        consumer = StubSharedMemoryConsumer()

        results: list[Any] = []

        # First run: produces SharedArray
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            results.append(wf.compute(result))

        # Second run: should load from cache (SharedArray → file → SharedArray)
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            results.append(wf.compute(result))

        import pandas as pd
        pd.testing.assert_frame_equal(results[0], results[1])


@pytest.mark.parametrize("entrypoint", ["create", "open", "load_image"])
def test_object_dtype_refused_before_shared_memory(entrypoint, monkeypatch):
    """Public helpers must refuse process-local references before any effect."""
    import numpy as np
    import bioimageflow_core._shared_storage as shared_storage
    import bioimageflow_core.io as image_io
    from bioimageflow_core import SharedArray
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    def no_segment(*args, **kwargs):
        raise AssertionError("Backing must not create or map object storage")

    monkeypatch.setattr(shared_storage, "create", no_segment)
    monkeypatch.setattr(shared_storage, "map_array", no_segment)
    ref = SharedArray("unused_object_segment", (1,), "object", "unopened_scope")
    if entrypoint == "create":
        operation = create_shared_output(np.array([object()], dtype=object))
    elif entrypoint == "open":
        operation = open_shared_array(ref)
    else:
        operation = image_io.load_image(
            ref, file_reader=lambda path: pytest.fail("SharedArray is not a Path")
        )
    with pytest.raises(ValueError, match="Python objects"):
        with operation:
            pytest.fail("Object-containing shared memory must not be yielded")


def test_structured_object_dtype_refused_before_creation(monkeypatch):
    """A nested object field is unsafe even when dtype.kind is not object."""
    import numpy as np
    import bioimageflow_core._shared_storage as shared_storage
    from bioimageflow_core.shm import create_shared_output

    def no_segment(*args, **kwargs):
        raise AssertionError("SharedMemory must not create object storage")

    monkeypatch.setattr(shared_storage, "create", no_segment)
    data = np.zeros(1, dtype=[("value", [("payload", object)])])
    with pytest.raises(ValueError, match="Python objects"):
        with create_shared_output(data):
            pytest.fail("Nested object storage must not be yielded")
