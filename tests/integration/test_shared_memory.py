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

from bioimageflow import Workflow

from .conftest import FileLoader, StubSharedMemoryConsumer, StubSharedMemoryTool


class TestSharedMemoryWorkflow:

    def test_shm_producer_then_consumer(self, tmp_workspace):
        """SharedArray flows from producer to consumer tool."""
        load = FileLoader()
        producer = StubSharedMemoryTool()
        consumer = StubSharedMemoryConsumer()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            df = wf.compute(result)

            assert len(df) == 3
            assert "num_labels" in df.columns
            # StubSharedMemoryTool creates zeros → only 1 unique label (0)
            assert all(df["num_labels"] == 1)


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

        # After outer context, the handle is closed but segment may still exist
        # (engine is responsible for unlinking)

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
            from multiprocessing.shared_memory import SharedMemory
            shm = SharedMemory(name=ref.name)
            shm.close()
            shm.unlink()


class TestLoadImageDispatch:

    def test_load_image_with_path(self, tmp_workspace):
        """load_image with a Path delegates to file_reader."""
        from bioimageflow_core.io import load_image

        img_path = tmp_workspace / "data" / "cell_01.tif"

        def reader(p):
            return p.read_text()

        with load_image(img_path, file_reader=reader) as data:
            assert data == "FAKE_IMAGE_cell_01.tif"

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
                from multiprocessing.shared_memory import SharedMemory
                shm = SharedMemory(name=ref.name)
                shm.close()
                shm.unlink()


class TestSaveImage:

    def test_save_image_delegates_to_writer(self, tmp_workspace):
        """save_image calls the provided file_writer with Path and data."""
        from bioimageflow_core.io import save_image

        out_path = tmp_workspace / "output.txt"
        save_image(out_path, "PIXEL_DATA", file_writer=lambda p, d: (p.write_text(d), None)[-1])

        assert out_path.exists()
        assert out_path.read_text() == "PIXEL_DATA"


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
        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            results.append(wf.compute(result))

        # Second run: should load from cache (SharedArray → file → SharedArray)
        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            shm_out = producer(input_image=raw["path"])
            result = consumer(label_map=shm_out["result"])
            results.append(wf.compute(result))

        import pandas as pd
        pd.testing.assert_frame_equal(results[0], results[1])
