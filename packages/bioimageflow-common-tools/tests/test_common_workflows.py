"""Integration tests for bioimageflow-common-tools.

These tests exercise the tool definitions, graph construction, and workflow
execution using generated data.
"""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest
from PIL import Image

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)
from bioimageflow import Workflow
from bioimageflow.validation import serialize_input_schema
from bioimageflow_common_tools import (
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
        img = np.arange(3 * 64 * 64, dtype=np.uint16).reshape(3, 64, 64)
        img = (img % 255).astype(np.uint8)
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
        with Workflow(engine="direct", storage_path=str(image_dir.parent / "bif")) as wf:
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

        with Workflow(engine="direct", storage_path=str(image_dir.parent / "bif")) as wf:
            node = Files()(path=str(image_dir), pattern="*.tif")
            result = wf.compute(node)
            assert len(result) == 2

    def test_explicit_files_preserve_order_and_ignore_pattern(
        self, image_dir: Path
    ) -> None:
        first = image_dir / "sample_02.tif"
        second = image_dir / "sample_01.tif"

        result = Files().transform(
            None,
            Arguments(
                path=None,
                files=[first, second],
                pattern="*.does-not-match",
                recursive=True,
            ),
        )

        assert result["path"].tolist() == [str(first), str(second)]

    def test_returns_resolved_absolute_paths(
        self, image_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(image_dir.parent)

        result = Files().transform(
            None,
            Arguments(
                path=Path("images"),
                files=None,
                pattern="*.tif",
                recursive=False,
            ),
        )

        assert all(Path(path).is_absolute() for path in result["path"])
        assert all(Path(path).is_file() for path in result["path"])

    def test_recursive_directory_scan(self, image_dir: Path) -> None:
        nested = image_dir / "nested"
        nested.mkdir()
        nested_file = nested / "sample_03.tif"
        nested_file.write_bytes(b"nested")

        result = Files().transform(
            None,
            Arguments(path=image_dir, files=None, pattern="*.tif", recursive=True),
        )

        assert nested_file in {Path(path) for path in result["path"]}

    def test_unmatched_scan_preserves_output_schema(self, image_dir: Path) -> None:
        result = Files().transform(
            None,
            Arguments(
                path=image_dir,
                files=None,
                pattern="*.does-not-exist",
                recursive=False,
            ),
        )

        assert result.empty
        assert list(result.columns) == ["path"]

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            (
                Arguments(path="/tmp", files=["/tmp/a.tif"], pattern="*", recursive=False),
                "Set either Directory or Files, not both.",
            ),
            (
                Arguments(path=None, files=None, pattern="*", recursive=False),
                "Set a Directory or at least one file.",
            ),
            (
                Arguments(path=None, files=[], pattern="*", recursive=False),
                "Set a Directory or at least one file.",
            ),
        ],
    )
    def test_rejects_invalid_source_selection(
        self, arguments: Arguments, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            Files().transform(None, arguments)

    def test_rejects_missing_and_non_file_entries(self, tmp_path: Path) -> None:
        directory = tmp_path / "directory"
        directory.mkdir()
        missing = tmp_path / "missing.tif"

        with pytest.raises(ValueError) as exc_info:
            Files().transform(
                None,
                Arguments(
                    path=None,
                    files=[missing, directory],
                    pattern="*",
                    recursive=False,
                ),
            )

        assert str(missing) in str(exc_info.value)
        assert str(directory) in str(exc_info.value)

    def test_rejects_non_directory_source(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.tif"
        file_path.write_bytes(b"x")

        with pytest.raises(ValueError, match="not a directory"):
            Files().transform(
                None,
                Arguments(
                    path=file_path,
                    files=None,
                    pattern="*",
                    recursive=False,
                ),
            )


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

    def test_rejects_mismatched_shapes(self, tmp_path: Path) -> None:
        labels = tmp_path / "labels.tif"
        reference = tmp_path / "reference.tif"
        iio.imwrite(labels, np.zeros((4, 4), dtype=np.uint16))
        iio.imwrite(reference, np.zeros((3, 4), dtype=np.uint16))

        with pytest.raises(ValueError, match="same shape"):
            LabelOverlaps().process_row(
                Arguments(label_image=labels, reference_image=reference)
            )

    @pytest.mark.parametrize(
        ("bad_value", "message"),
        [(-1.0, "non-negative"), (1.5, "integer-valued"), (np.nan, "finite")],
    )
    def test_rejects_invalid_label_values(
        self, tmp_path: Path, bad_value: float, message: str
    ) -> None:
        labels = np.zeros((3, 3), dtype=np.float32)
        labels[1, 1] = bad_value
        labels_path = tmp_path / "labels.tif"
        reference_path = tmp_path / "reference.tif"
        iio.imwrite(labels_path, labels)
        iio.imwrite(reference_path, np.zeros((3, 3), dtype=np.uint16))

        with pytest.raises(ValueError, match=message):
            LabelOverlaps().process_row(
                Arguments(label_image=labels_path, reference_image=reference_path)
            )

    def test_rejects_float_label_above_uint64_range(self, tmp_path: Path) -> None:
        labels_path = tmp_path / "labels.tif"
        reference_path = tmp_path / "reference.tif"
        iio.imwrite(labels_path, np.array([[float(2**64)]], dtype=np.float64))
        iio.imwrite(reference_path, np.zeros((1, 1), dtype=np.uint16))

        with pytest.raises(ValueError, match="larger than uint64"):
            LabelOverlaps().process_row(
                Arguments(label_image=labels_path, reference_image=reference_path)
            )


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
            row_consumption = RowConsumption.MAPPED
            display_name = "Binary Producer"
            environment = EnvironmentSpec(name="stub", dependencies={})

            class Inputs(IOModel):
                pass

            class Outputs(IOModel):
                output_image: Annotated[
                    Path,
                    ImageSpec(semantics={Semantic.BINARY}),
                ] = Template("binary.tif")

            def process_row(self, arguments: Any, *, context: object | None = None) -> Any:
                raise AssertionError("graph construction test only")

        with Workflow(engine="direct", storage_path=str(tmp_path / "bif")):
            binary = BinaryProducer()(name="binary")
            Mosaic()(input_image=binary["output_image"], name="mosaic")

    def test_empty_batch_returns_no_outputs(self) -> None:
        assert Mosaic().process_batch([]) == []

    def test_uses_largest_cell_for_heterogeneous_grayscale_tiles(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "first.png"
        second = tmp_path / "second.png"
        output = tmp_path / "mosaic.png"
        iio.imwrite(first, np.full((2, 3), 50, dtype=np.uint8))
        iio.imwrite(second, np.full((4, 1), 100, dtype=np.uint8))
        arguments = [
            Arguments(
                input_image=path,
                columns=2,
                tile_width=None,
                tile_height=None,
                mosaic_path=output,
            )
            for path in (first, second)
        ]

        result = Mosaic().process_batch(arguments)

        with Image.open(result[0].mosaic_path) as mosaic:
            assert mosaic.mode == "L"
            assert mosaic.size == (6, 4)
        assert all(row.image_count == 2 for row in result)

    def test_preserves_rgb_and_rgba_modes(self, tmp_path: Path) -> None:
        rgb_path = tmp_path / "rgb.png"
        rgba_path = tmp_path / "rgba.png"
        output = tmp_path / "mosaic.png"
        rgb = np.zeros((2, 2, 3), dtype=np.uint8)
        rgb[..., 1] = 200
        rgba = np.zeros((2, 2, 4), dtype=np.uint8)
        rgba[..., 0] = 255
        rgba[..., 3] = 128
        iio.imwrite(rgb_path, rgb)
        iio.imwrite(rgba_path, rgba)
        arguments = [
            Arguments(
                input_image=path,
                columns=2,
                tile_width=None,
                tile_height=None,
                mosaic_path=output,
            )
            for path in (rgb_path, rgba_path)
        ]

        Mosaic().process_batch(arguments)

        with Image.open(output) as mosaic:
            assert mosaic.mode == "RGBA"
            assert mosaic.size == (4, 2)
            assert mosaic.getpixel((0, 0)) == (0, 200, 0, 255)
            assert mosaic.getpixel((2, 0)) == (255, 0, 0, 128)

    def test_preserves_uint16_grayscale_values(self, tmp_path: Path) -> None:
        input_path = tmp_path / "uint16.tif"
        output = tmp_path / "mosaic.png"
        pixels = np.array([[0, 256], [1000, 65535]], dtype=np.uint16)
        iio.imwrite(input_path, pixels)

        Mosaic().process_batch([
            Arguments(
                input_image=input_path,
                columns=1,
                tile_width=None,
                tile_height=None,
                mosaic_path=output,
            )
        ])

        with Image.open(output) as mosaic:
            assert mosaic.mode == "I;16"
            np.testing.assert_array_equal(np.asarray(mosaic), pixels)

    def test_preserves_palette_transparency(self, tmp_path: Path) -> None:
        input_path = tmp_path / "palette.png"
        output = tmp_path / "mosaic.png"
        palette = Image.new("P", (2, 1))
        palette.putpalette([255, 0, 0, 0, 255, 0] + [0] * (256 * 3 - 6))
        palette.putdata([0, 1])
        palette.info["transparency"] = 0
        palette.save(input_path)

        Mosaic().process_batch([
            Arguments(
                input_image=input_path,
                columns=1,
                tile_width=None,
                tile_height=None,
                mosaic_path=output,
            )
        ])

        with Image.open(output) as mosaic:
            assert mosaic.mode == "RGBA"
            assert mosaic.getpixel((0, 0)) == (255, 0, 0, 0)
            assert mosaic.getpixel((1, 0)) == (0, 255, 0, 255)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("columns", 1.5, "Columns must be a positive integer"),
            ("tile_width", 0, "Tile width must be a positive integer"),
            ("tile_height", True, "Tile height must be a positive integer"),
        ],
    )
    def test_rejects_non_positive_integer_layout_settings(
        self,
        tmp_path: Path,
        field: str,
        value: object,
        message: str,
    ) -> None:
        input_path = tmp_path / "input.png"
        iio.imwrite(input_path, np.zeros((2, 2), dtype=np.uint8))
        values = {
            "input_image": input_path,
            "columns": 1,
            "tile_width": None,
            "tile_height": None,
            "mosaic_path": tmp_path / "mosaic.png",
        }
        values[field] = value

        with pytest.raises(ValueError, match=message):
            Mosaic().process_batch([Arguments(**values)])
