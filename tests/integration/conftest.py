"""
Shared fixtures and stub tool definitions for integration tests.

Stub tools are minimal implementations that simulate real bioimage tools
without requiring heavy dependencies (cellpose, stardist, etc.).
They exercise the full BioImageFlow API surface.
"""

from pathlib import Path
from typing import Annotated, Any

import pytest

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    GUIMeta,
    IOModel,
    ImageSpec,
    ProcessingTool,
    ResourceSpec,
    Semantic,
    SharedArray,
)
from bioimageflow import (
    DataFrameTool,
    Passthrough,
    Workflow,
)


# ---------------------------------------------------------------------------
# Shared environment specs
# ---------------------------------------------------------------------------

cellpose_env = EnvironmentSpec(
    name="cellpose",
    dependencies={"conda": ["cellpose==4.0.8"], "python": "3.12"},
)

stardist_env = EnvironmentSpec(
    name="stardist",
    dependencies={"conda": ["stardist==0.9", "tensorflow"], "python": "3.11"},
)

imageio_env = EnvironmentSpec(
    name="imageio",
    dependencies={"conda": ["imageio"], "python": "3.12"},
)

numpy_env = EnvironmentSpec(
    name="numpy_only",
    dependencies={"conda": ["numpy"], "python": "3.12"},
)


# ---------------------------------------------------------------------------
# Source tools (DataFrameTool)
# ---------------------------------------------------------------------------

class FileLoader(DataFrameTool):
    """List image files in a directory. Acts as a source node."""
    name = "file_loader"
    tags = ["source", "loader"]

    class Inputs(IOModel):
        path: str

    class Outputs(IOModel):
        path: Path
        filename: str

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd
        directory = Path(arguments.path)
        files = sorted(directory.glob("*"))
        rows = [{"path": f, "filename": f.name} for f in files if f.is_file()]
        return pd.DataFrame(rows)


class CsvLoader(DataFrameTool):
    """Load a CSV file as a DataFrame. Acts as a source node."""
    name = "csv_loader"
    tags = ["source", "csv"]

    class Inputs(IOModel):
        path: str

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd
        return pd.read_csv(arguments.path)


# ---------------------------------------------------------------------------
# Processing tools
# ---------------------------------------------------------------------------

class StubSegmenter(ProcessingTool):
    """Simulates cell segmentation. Writes a small file as output."""
    name = "stub_segmenter"
    tags = ["segmentation"]
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: Annotated[float, GUIMeta(connectable=False, min=1.0, max=500.0, step=0.5)] = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = "{input_image.stem}_mask_{row_index}.png"  # type: ignore[assignment]
        cell_count: int

    def process_row(self, arguments: Arguments) -> Any:
        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        mask_path.write_text("STUB_MASK_DATA")
        return self.Outputs(mask=mask_path, cell_count=42)


class StubStats(ProcessingTool):
    """Simulates intensity measurement on image + mask."""
    name = "stub_stats"
    tags = ["measurement"]
    environment = imageio_env

    class Inputs(IOModel):
        image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]

    class Outputs(IOModel):
        mean_intensity: float
        area: int

    def process_row(self, arguments: Arguments) -> Any:
        return self.Outputs(mean_intensity=128.5, area=1024)


class StubTiler(ProcessingTool):
    """Simulates tiling: 1-to-N output (splits one image into tiles)."""
    name = "stub_tiler"
    tags = ["tiling"]
    environment = imageio_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        tile_count: Annotated[int, GUIMeta(connectable=False, min=1, max=64, step=1)] = 4

    class Outputs(IOModel):
        tile: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})] = "{input_image.stem}_tile_{row_index}.png"  # type: ignore[assignment]

    def process_row(self, arguments: Arguments) -> Any:
        base = Path(arguments.tile)
        results = []
        for i in range(arguments.tile_count):
            tile_path = base.with_name(f"{base.stem}_part{i}{base.suffix}")
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_path.write_text(f"TILE_{i}")
            results.append(self.Outputs(tile=tile_path))
        return results


class StubBatchProcessor(ProcessingTool):
    """Uses process_batch instead of process_row for GPU-style batching."""
    name = "stub_batch_processor"
    tags = ["batch", "gpu"]
    environment = numpy_env
    resources = ResourceSpec(gpu=1, max_concurrent=2)

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        embedding: Path = "{input_image.stem}_embed_{row_index}.npy"  # type: ignore[assignment]

    def process_batch(self, arguments_list: list[Any]) -> Any:
        results = []
        for args in arguments_list:
            out_path = Path(args.embedding)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("EMBED_DATA")
            results.append(self.Outputs(embedding=out_path))
        return results  # list[Outputs] — 1-to-1, auto-wrapped by engine


class StubBatchExploder(ProcessingTool):
    """Uses process_batch with 1-to-N outputs."""
    name = "stub_batch_exploder"
    tags = ["batch"]
    environment = numpy_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        crop: Path = "{input_image.stem}_crop_{row_index}.png"  # type: ignore[assignment]

    def process_batch(self, arguments_list: list[Any]) -> Any:
        results = []
        for args in arguments_list:
            base = Path(args.crop)
            row_outputs = []
            for i in range(2):  # Each row produces 2 crops
                crop_path = base.with_name(f"{base.stem}_c{i}{base.suffix}")
                crop_path.parent.mkdir(parents=True, exist_ok=True)
                crop_path.write_text(f"CROP_{i}")
                row_outputs.append(self.Outputs(crop=crop_path))
            results.append(row_outputs)
        return results  # list[list[Outputs]] — 1-to-N


class StubSourceProcessingTool(ProcessingTool):
    """A ProcessingTool used as a source node (no upstream, only constants)."""
    name = "stub_source_processor"
    tags = ["source"]
    environment = imageio_env

    class Inputs(IOModel):
        directory: str

    class Outputs(IOModel):
        path: Path
        metadata: str

    def process_row(self, arguments: Arguments) -> Any:
        directory = Path(arguments.directory)
        results = []
        for f in sorted(directory.glob("*")):
            if f.is_file():
                results.append(self.Outputs(path=f, metadata=f.stem))
        return results


class StubRegistration(ProcessingTool):
    """Simulates image registration from two inputs."""
    name = "stub_registration"
    tags = ["registration"]
    environment = imageio_env

    class Inputs(IOModel):
        fixed: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        moving: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        registered: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})] = "{fixed.stem}_registered_{row_index}.tif"  # type: ignore[assignment]
        displacement: Annotated[Path, ImageSpec(semantics={Semantic.DISPLACEMENT})] = "{fixed.stem}_disp_{row_index}.tif"  # type: ignore[assignment]

    def process_row(self, arguments: Arguments) -> Any:
        reg_path = Path(arguments.registered)
        disp_path = Path(arguments.displacement)
        for p in (reg_path, disp_path):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("STUB_DATA")
        return self.Outputs(registered=reg_path, displacement=disp_path)


class StubSharedMemoryTool(ProcessingTool):
    """Produces shared memory output instead of files."""
    name = "stub_shm_tool"
    tags = ["shared_memory"]
    environment = numpy_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    def process_row(self, arguments: Arguments) -> Any:
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        data = np.zeros((64, 64), dtype=np.uint16)
        with create_shared_output(data) as shm_ref:
            return self.Outputs(result=shm_ref)


class StubSharedMemoryConsumer(ProcessingTool):
    """Consumes shared memory input."""
    name = "stub_shm_consumer"
    tags = ["shared_memory"]
    environment = numpy_env

    class Inputs(IOModel):
        label_map: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    class Outputs(IOModel):
        num_labels: int

    def process_row(self, arguments: Arguments) -> Any:
        from bioimageflow_core.io import load_image

        def noop_reader(p: Path) -> Any:
            raise RuntimeError("Should not be called for SharedArray")

        with load_image(arguments.label_map, file_reader=noop_reader) as arr:
            unique_count = len(set(arr.flat))
        return self.Outputs(num_labels=unique_count)


# ---------------------------------------------------------------------------
# Tool families
# ---------------------------------------------------------------------------

class CellposeBase(ProcessingTool):
    """Base class for the Cellpose tool family. Shares environment."""
    environment = cellpose_env
    tags = ["cellpose"]


class CellposeSegmenter(CellposeBase):
    name = "cellpose_segmenter"
    documentation = "Segments cells using the Cellpose algorithm."

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: Annotated[float, GUIMeta(connectable=False, min=1.0, max=500.0, step=0.5)] = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = "{input_image.stem}_mask_{row_index}.png"  # type: ignore[assignment]
        cell_count: int

    def process_row(self, arguments: Arguments) -> Any:
        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        mask_path.write_text("CELLPOSE_MASK")
        return self.Outputs(mask=mask_path, cell_count=55)


class CellposeTrain(CellposeBase):
    name = "cellpose_train"
    documentation = "Trains a custom Cellpose model."
    tags = ["cellpose", "training"]

    class Inputs(IOModel):
        training_images: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        training_masks: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        epochs: Annotated[int, GUIMeta(connectable=False, min=1, max=10000, step=10)] = 100

    class Outputs(IOModel):
        model_path: Path = "{node_name}_model"  # type: ignore[assignment]

    def process_batch(self, arguments_list: list[Any]) -> Any:
        results = []
        for args in arguments_list:
            out = Path(args.model_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("TRAINED_MODEL")
            results.append(self.Outputs(model_path=out))
        return results


class StardistSegmenter(ProcessingTool):
    """Segmenter using a different environment (stardist)."""
    name = "stardist_segmenter"
    environment = stardist_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = "{input_image.stem}_stardist_{row_index}.png"  # type: ignore[assignment]

    def process_row(self, arguments: Arguments) -> Any:
        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        mask_path.write_text("STARDIST_MASK")
        return self.Outputs(mask=mask_path)


# ---------------------------------------------------------------------------
# DataFrameTools (transform)
# ---------------------------------------------------------------------------

class ColumnRegex(DataFrameTool):
    """Create dynamically named columns from a regex pattern."""
    name = "column_regex"
    tags = ["dataframe", "regex"]

    class Inputs(IOModel):
        column_name: str
        regex: str = r"(?P<column1>\w+)_(?P<column2>\w+)"

    def transform(self, df: Any, arguments: Any) -> Any:
        import re
        df = df.copy()
        for index, row in df.iterrows():
            m = re.search(arguments.regex, str(row[arguments.column_name]))
            if m:
                for key, value in m.groupdict().items():
                    df.at[index, key] = value
        return df


class FilterRows(DataFrameTool):
    """Filter DataFrame rows by column value constraints."""
    name = "filter_rows"
    tags = ["dataframe", "filter"]

    class Outputs(Passthrough):
        pass

    class Inputs(IOModel):
        column_name: str
        min: float | None = None
        max: float | None = None

    def transform(self, df: Any, arguments: Any) -> Any:
        if arguments.min is not None:
            df = df[df[arguments.column_name] >= arguments.min]
        if arguments.max is not None:
            df = df[df[arguments.column_name] <= arguments.max]
        return df


class AddColumn(DataFrameTool):
    """Add a constant column to the DataFrame."""
    name = "add_column"
    tags = ["dataframe"]

    class Outputs(Passthrough):
        pass

    class Inputs(IOModel):
        column_name: str
        value: str

    def transform(self, df: Any, arguments: Any) -> Any:
        df = df.copy()
        df[arguments.column_name] = arguments.value
        return df


class CountLabelOverlaps(DataFrameTool):
    """Count the number of overlapping labels."""
    name = "count_label_overlaps"
    tags = ["aggregation"]

    class Inputs(IOModel):
        label1_min: float | None = None
        label1_max: float | None = None
        average: bool = False

    class Outputs(IOModel):
        image1: str
        label1: int
        label2_count: int

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd
        if arguments.label1_min is not None:
            df = df[df["label1"] >= arguments.label1_min]
        if not {"label1", "image1", "label2"}.issubset(df.columns):
            return pd.DataFrame()
        result = (
            df.groupby(["image1", "label1"])["label2"]
            .agg(lambda x: (x != 0).sum())
            .reset_index(name="label2_count")
        )
        return result


class PrepareRegistration(DataFrameTool):
    """Pair each image with its reference for registration."""
    name = "prepare_registration"

    class Inputs(IOModel):
        reference_index: int = 0

    class Outputs(IOModel):
        image_path: Path
        reference_path: Path

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd
        ref_path = df.iloc[arguments.reference_index]["path"]
        rows = []
        for _, row in df.iterrows():
            if row["path"] != ref_path:
                rows.append({"image_path": row["path"], "reference_path": ref_path})
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_wetlands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable Wetlands in all tests — stub tools run in the main process."""
    original_init = Workflow.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("use_wetlands", False)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Workflow, "__init__", patched_init)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with sample image files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["cell_01.tif", "cell_02.tif", "cell_03.tif"]:
        (data_dir / name).write_text(f"FAKE_IMAGE_{name}")
    return tmp_path


@pytest.fixture
def tmp_workspace_with_metadata(tmp_path: Path) -> Path:
    """Create a workspace with files named for metadata extraction."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for patient, slc in [("patientA", "001"), ("patientA", "002"), ("patientB", "001")]:
        (data_dir / f"{patient}_{slc}.tif").write_text("FAKE")
    return tmp_path


@pytest.fixture
def tmp_workspace_two_sources(tmp_path: Path) -> Path:
    """Create two separate data directories for multi-source tests."""
    mri_dir = tmp_path / "mri"
    ct_dir = tmp_path / "ct"
    mri_dir.mkdir()
    ct_dir.mkdir()
    for pid in ["P001", "P002", "P003"]:
        (mri_dir / f"{pid}_mri.nii").write_text("MRI_DATA")
        (ct_dir / f"{pid}_ct.nii").write_text("CT_DATA")

    csv_path = tmp_path / "patients.csv"
    csv_path.write_text("patient_id,age,sex\nP001,65,M\nP002,42,F\nP003,71,M\n")
    return tmp_path


@pytest.fixture
def tmp_workspace_with_quality(tmp_path: Path) -> Path:
    """Workspace with a CSV containing quality scores."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for i in range(5):
        (data_dir / f"img_{i:03d}.tif").write_text("FAKE")

    csv_path = tmp_path / "quality.csv"
    csv_path.write_text(
        "filename,quality\nimg_000.tif,0.9\nimg_001.tif,0.3\nimg_002.tif,0.8\nimg_003.tif,0.2\nimg_004.tif,0.7\n"
    )
    return tmp_path
