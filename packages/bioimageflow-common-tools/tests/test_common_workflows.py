"""Integration tests for bioimageflow-common-tools.

These tests exercise the tool definitions, graph construction, and
workflow execution using stub data. They do NOT require heavy deps
(bioio, cellpose, SimpleITK, atlas CLI) — processing tools that call
external libs are tested for graph construction only.
"""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)
from bioimageflow import Workflow
from bioimageflow.validation import serialize_input_schema
from bioimageflow_common_tools import (
    ExtractChannel,
    Files,
    LabelOverlaps,
    Mosaic,
)


pytestmark = pytest.mark.package_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    """Create a directory with fake multi-channel TIFF images."""
    data_dir = tmp_path / "images"
    data_dir.mkdir()
    for name in ["sample_01.tif", "sample_02.tif"]:
        # 3-channel image (CYX): 3 x 64 x 64
        img = np.random.randint(0, 255, (3, 64, 64), dtype=np.uint8)
        iio.imwrite(str(data_dir / name), img, photometric="minisblack")
    return data_dir


@pytest.fixture
def label_images(tmp_path: Path) -> tuple[Path, Path]:
    """Create a pair of label images with known overlaps."""
    # Reference: 3 nuclei (labels 1, 2, 3)
    reference = np.zeros((64, 64), dtype=np.uint32)
    reference[5:25, 5:25] = 1
    reference[5:25, 30:55] = 2
    reference[35:55, 15:45] = 3

    # Spots: 5 spots overlapping with nuclei
    spots = np.zeros((64, 64), dtype=np.uint32)
    spots[10:12, 10:12] = 1   # in nucleus 1
    spots[15:17, 15:17] = 2   # in nucleus 1
    spots[10:12, 35:37] = 3   # in nucleus 2
    spots[40:42, 20:22] = 4   # in nucleus 3
    spots[45:47, 30:32] = 5   # in nucleus 3

    ref_path = tmp_path / "reference.tif"
    spots_path = tmp_path / "spots.tif"
    iio.imwrite(str(ref_path), reference)
    iio.imwrite(str(spots_path), spots)

    return spots_path, ref_path


# ---------------------------------------------------------------------------
# Files tool
# ---------------------------------------------------------------------------

class TestFiles:
    def test_lists_files(self, image_dir: Path) -> None:
        with Workflow(storage_path=str(image_dir.parent / "bif"), use_wetlands=False) as wf:
            node = Files()(path=str(image_dir))
            result = wf.compute(node)
            assert len(result) == 2
            assert list(result.columns) == ["path"]
            assert {Path(p).name for p in result["path"]} == {
                "sample_01.tif",
                "sample_02.tif",
            }

    def test_glob_pattern(self, image_dir: Path) -> None:
        # Create a non-matching file
        (image_dir / "notes.txt").write_text("hello")

        with Workflow(storage_path=str(image_dir.parent / "bif"), use_wetlands=False) as wf:
            node = Files()(path=str(image_dir), pattern="*.tif")
            result = wf.compute(node)
            assert len(result) == 2


# ---------------------------------------------------------------------------
# ExtractChannel tool
# ---------------------------------------------------------------------------

class TestExtractChannel:
    def test_extracts_channel(self, image_dir: Path) -> None:
        with Workflow(storage_path=str(image_dir.parent / "bif"), use_wetlands=False) as wf:
            files = Files()(path=str(image_dir), pattern="*.tif")
            ch0 = ExtractChannel()(input_image=files["path"], channel=0)
            result = wf.compute(ch0)
            assert len(result) == 2
            assert "output_image" in result.columns

            # Verify the output is a 2D image (single channel)
            out_path = result.iloc[0]["output_image"]
            img = iio.imread(str(out_path))
            assert img.ndim == 2
            assert img.shape == (64, 64)

    def test_extracts_different_channels(self, image_dir: Path) -> None:
        with Workflow(storage_path=str(image_dir.parent / "bif"), use_wetlands=False) as wf:
            files = Files()(path=str(image_dir), pattern="*.tif")
            ch0 = ExtractChannel()(
                input_image=files["path"], channel=0, name="ch0"
            )
            ch2 = ExtractChannel()(
                input_image=files["path"], channel=2, name="ch2"
            )
            results = wf.compute(ch0, ch2)
            assert len(results) == 2
            assert len(results["ch0"]) == 2
            assert len(results["ch2"]) == 2


# ---------------------------------------------------------------------------
# LabelOverlaps tool
# ---------------------------------------------------------------------------

class TestLabelOverlaps:
    def test_computes_overlaps(
        self, tmp_path: Path, label_images: tuple[Path, Path]
    ) -> None:
        spots_path, ref_path = label_images

        tool = LabelOverlaps()

        args = Arguments(
            label_image=str(spots_path),
            reference_image=str(ref_path),
        )
        result = tool.process_row(args)

        df = pd.DataFrame([vars(row) for row in result])
        assert "reference_label" in df.columns
        assert "spot_label" in df.columns
        assert "overlap_count" in df.columns

        # Filter out background (spot_label=0) to check actual spot assignments
        real = df[df["spot_label"] > 0]

        # Spot 1 and 2 are in nucleus 1, spot 3 in nucleus 2, spots 4&5 in nucleus 3
        nucleus_1_spots = real[real["reference_label"] == 1]
        assert set(nucleus_1_spots["spot_label"]) == {1, 2}

        nucleus_2_spots = real[real["reference_label"] == 2]
        assert 3 in set(nucleus_2_spots["spot_label"])

        nucleus_3_spots = real[real["reference_label"] == 3]
        assert set(nucleus_3_spots["spot_label"]) == {4, 5}


# ---------------------------------------------------------------------------
# Mosaic tool
# ---------------------------------------------------------------------------

class TestMosaic:
    def test_input_schema_declares_scalar_image_semantics(self) -> None:
        schema = serialize_input_schema(Mosaic)
        semantics = schema["input_image"]["image_spec"]["semantics"]
        assert semantics == ["binary", "intensity", "label", "probability"]

    def test_accepts_binary_images_for_visualization(self, tmp_path: Path) -> None:
        class BinaryProducer(ProcessingTool):
            display_name = "Binary Producer"
            environment = EnvironmentSpec(name="stub", dependencies={})

            class Inputs(IOModel):
                pass

            class Outputs(IOModel):
                output_image: Annotated[
                    Path,
                    ImageSpec(semantics={Semantic.BINARY}),
                ] = Template("binary.tif")

            def process_row(self, arguments: Any) -> Any:
                raise AssertionError("graph construction test only")

        with Workflow(storage_path=str(tmp_path / "bif"), use_wetlands=False):
            binary = BinaryProducer()(name="binary")
            Mosaic()(input_image=binary["output_image"], name="mosaic")


# ---------------------------------------------------------------------------
# Full pipeline (Files → ExtractChannel → LabelOverlaps → Stats)
# ---------------------------------------------------------------------------

class TestMiniPipeline:
    def test_full_pipeline(
        self, tmp_path: Path, image_dir: Path
    ) -> None:
        """Test Files → ExtractChannel pipeline (branching topology)."""
        with Workflow(storage_path=str(tmp_path / "bif"), use_wetlands=False) as wf:
            files = Files()(path=str(image_dir), pattern="*.tif")
            ch0 = ExtractChannel()(
                input_image=files["path"], channel=0, name="ch0"
            )
            ch1 = ExtractChannel()(
                input_image=files["path"], channel=1, name="ch1"
            )
            ch2 = ExtractChannel()(
                input_image=files["path"], channel=2, name="ch2"
            )
            results = wf.compute(ch0, ch1, ch2)
            # 2 images × 3 channels = 6 outputs total
            for name in ["ch0", "ch1", "ch2"]:
                assert len(results[name]) == 2
                for out_path in results[name]["output_image"]:
                    assert Path(out_path).exists()

    def test_overlap_to_stats_pipeline(
        self, tmp_path: Path, label_images: tuple[Path, Path]
    ) -> None:
        """Test LabelOverlaps pipeline with stub labeler.

        Simulates the FISH pattern: a single 2-channel image where ch0
        is spot labels and ch1 is nucleus labels. Uses StubLabeler to
        produce output with LABEL semantics.
        """
        spots_path, ref_path = label_images

        stub_env = EnvironmentSpec(
            name="stub", dependencies={"pip": ["numpy"]}
        )

        class StubLabeler(ProcessingTool):
            """Passthrough that re-tags an image as LABEL semantic."""
            display_name = "Stub Labeler"
            environment = stub_env

            class Inputs(IOModel):
                input_image: Annotated[
                    Path,
                    ImageSpec(semantics={Semantic.INTENSITY}),
                ]

            class Outputs(IOModel):
                output_image: Annotated[
                    Path,
                    ImageSpec(
                        semantics={Semantic.LABEL},
                        layouts={Layout.PLANAR},
                    ),
                ] = Template("{input_image.stem}_labeled{ext}")

            def process_row(self, arguments: Any) -> Any:
                import shutil
                out = Path(arguments.output_image)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(arguments.input_image), str(out))
                return self.Outputs(output_image=out)

        # Stack spots and reference as a 2-channel image
        spots_data = iio.imread(str(spots_path))
        ref_data = iio.imread(str(ref_path))
        combined = np.stack([spots_data, ref_data], axis=0)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        iio.imwrite(str(data_dir / "sample.tif"), combined)

        with Workflow(storage_path=str(tmp_path / "bif"), use_wetlands=False) as wf:
            files = Files()(path=str(data_dir))
            ch_spots = ExtractChannel()(
                input_image=files["path"], channel=0, name="ch_spots"
            )
            ch_refs = ExtractChannel()(
                input_image=files["path"], channel=1, name="ch_refs"
            )
            # Re-tag as LABEL (simulating segmentation label producers)
            labeled_spots = StubLabeler()(
                input_image=ch_spots["output_image"], name="label_spots"
            )
            labeled_refs = StubLabeler()(
                input_image=ch_refs["output_image"], name="label_refs"
            )
            overlaps = LabelOverlaps()(
                label_image=labeled_spots["output_image"],
                reference_image=labeled_refs["output_image"],
            )
            result = wf.compute(overlaps)
            assert {"reference_label", "spot_label", "overlap_count"}.issubset(
                result.columns
            )
            real = result[result["spot_label"] > 0]
            assert len(real) > 0
