"""Execution and graph-construction tests for segmentation package tools."""

from __future__ import annotations

from pathlib import Path
import sys
import types

import imageio.v3 as iio
import numpy as np
import pytest
from skimage.filters import threshold_otsu

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import (
    Cellpose3,
    CellposeSAM,
    DistanceWatershedSegment,
    FilterLabels,
    LocalThresholdSegment,
    OtsuThresholdSegment,
    PostprocessLabels,
    SplitTouchingObjects,
    StarDistSegmenter,
    ThresholdSegment,
    WatershedSegment,
)

pytestmark = pytest.mark.package_tools


def test_threshold_segment_labels_synthetic_objects(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "labels.tif"
    image = np.zeros((8, 8), dtype=np.uint8)
    image[1:3, 1:3] = 10
    image[5:7, 5:7] = 10
    iio.imwrite(image_path, image)

    result = ThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, threshold=5.0, above=True)
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_threshold_segment_uses_strict_threshold_and_face_connectivity(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "labels.tif"
    image = np.array([[5, 0], [0, 6]], dtype=np.uint8)
    iio.imwrite(image_path, image)

    result = ThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, threshold=5.0, above=True)
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 1
    assert labels[0, 0] == 0
    assert labels[1, 1] == 1


def test_threshold_segment_keeps_diagonal_objects_separate(tmp_path: Path) -> None:
    image_path = tmp_path / "diagonal.tif"
    labels_path = tmp_path / "labels.tif"
    image = np.array([[1, 0], [0, 1]], dtype=np.uint8)
    iio.imwrite(image_path, image)

    result = ThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, threshold=0.0, above=True)
    )

    assert result.object_count == 2
    assert set(np.unique(iio.imread(labels_path))) == {0, 1, 2}


def test_cellpose_sam_executes_with_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cellpose_module = types.ModuleType("cellpose")
    models_module = types.ModuleType("cellpose.models")
    created_models: list[str] = []

    class FakeCellposeModel:
        def __init__(self, pretrained_model: str) -> None:
            self.pretrained_model = pretrained_model
            created_models.append(pretrained_model)

        def eval(self, image: np.ndarray, **_: object) -> tuple[np.ndarray]:
            labels = np.zeros(image.shape[-2:], dtype=np.uint32)
            labels[2:5, 2:5] = 1
            labels[6:8, 6:8] = 7
            return (labels,)

    models_module.CellposeModel = FakeCellposeModel
    cellpose_module.models = models_module
    monkeypatch.setitem(sys.modules, "cellpose", cellpose_module)
    monkeypatch.setitem(sys.modules, "cellpose.models", models_module)

    image_path = tmp_path / "input.tif"
    iio.imwrite(image_path, np.zeros((10, 10), dtype=np.float32))
    tool = CellposeSAM()
    result = tool.process_row(
        Arguments(
            input_image=image_path,
            pretrained_model="cpsam_v2",
            diameter=0.0,
            channel_axis="last",
            flow_threshold=0.4,
            cellprob_threshold=0.0,
            mask=tmp_path / "mask.tif",
        )
    )
    second_result = tool.process_row(
        Arguments(
            input_image=image_path,
            pretrained_model="cpsam_v2",
            diameter=12.0,
            channel_axis="last",
            flow_threshold=0.7,
            cellprob_threshold=-1.0,
            mask=tmp_path / "second_mask.tif",
        )
    )

    assert result.cell_count == 2
    assert set(np.unique(iio.imread(result.mask))) == {0, 1, 7}
    assert second_result.cell_count == 2
    assert created_models == ["cpsam_v2"]


def test_cellpose3_model_cache_is_keyed_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cellpose_module = types.ModuleType("cellpose")
    models_module = types.ModuleType("cellpose.models")
    created_models: list[str] = []

    class FakeCellpose:
        def __init__(self, model_type: str) -> None:
            self.model_type = model_type
            created_models.append(model_type)

    models_module.Cellpose = FakeCellpose
    cellpose_module.models = models_module
    monkeypatch.setitem(sys.modules, "cellpose", cellpose_module)
    monkeypatch.setitem(sys.modules, "cellpose.models", models_module)
    tool = Cellpose3()

    first = tool._get_model("cyto3")
    assert tool._get_model("cyto3") is first
    second = tool._get_model("nuclei")

    assert second is not first
    assert created_models == ["cyto3", "nuclei"]

    tool.clear_model_cache()
    assert tool._get_model("nuclei") is not second
    assert created_models == ["cyto3", "nuclei", "nuclei"]


def test_cellpose3_rejects_channel_selector_outside_declared_axis(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "two_channels.tif"
    iio.imwrite(image_path, np.zeros((8, 9, 2), dtype=np.float32))

    with pytest.raises(ValueError, match=r"channel=3 is out of range for 2 channels"):
        Cellpose3().process_row(
            Arguments(
                input_image=image_path,
                diameter=10.0,
                model_type="cyto3",
                channel=3,
                nuclear_channel=0,
                channel_axis="last",
                flow_threshold=0.4,
                cellprob_threshold=0.0,
                mask=tmp_path / "mask.tif",
            )
        )


def test_cellpose_sam_model_cache_is_keyed_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cellpose_module = types.ModuleType("cellpose")
    models_module = types.ModuleType("cellpose.models")
    created_models: list[str] = []

    class FakeCellposeModel:
        def __init__(self, pretrained_model: str) -> None:
            self.pretrained_model = pretrained_model
            created_models.append(pretrained_model)

    models_module.CellposeModel = FakeCellposeModel
    cellpose_module.models = models_module
    monkeypatch.setitem(sys.modules, "cellpose", cellpose_module)
    monkeypatch.setitem(sys.modules, "cellpose.models", models_module)
    tool = CellposeSAM()

    first = tool._get_model("cpsam_v2")
    assert tool._get_model("cpsam_v2") is first
    second = tool._get_model("alternate")

    assert second is not first
    assert created_models == ["cpsam_v2", "alternate"]

    tool.clear_model_cache()
    assert tool._get_model("alternate") is not second
    assert created_models == ["cpsam_v2", "alternate", "alternate"]


def test_stardist_model_cache_is_keyed_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stardist_module = types.ModuleType("stardist")
    models_module = types.ModuleType("stardist.models")
    created_models: list[str] = []

    class FakeStarDist2D:
        @classmethod
        def from_pretrained(cls, model_name: str) -> object:
            created_models.append(model_name)
            return object()

    models_module.StarDist2D = FakeStarDist2D
    stardist_module.models = models_module
    monkeypatch.setitem(sys.modules, "stardist", stardist_module)
    monkeypatch.setitem(sys.modules, "stardist.models", models_module)
    tool = StarDistSegmenter()

    first = tool._get_model("2D_versatile_fluo")
    assert tool._get_model("2D_versatile_fluo") is first
    second = tool._get_model("2D_versatile_he")

    assert second is not first
    assert created_models == ["2D_versatile_fluo", "2D_versatile_he"]

    tool.clear_model_cache()
    assert tool._get_model("2D_versatile_he") is not second
    assert created_models == [
        "2D_versatile_fluo",
        "2D_versatile_he",
        "2D_versatile_he",
    ]


def test_stardist_prepares_explicit_channel_axes() -> None:
    channel_first = np.zeros((2, 5, 7), dtype=np.float32)
    channel_first[1] = 3.0
    channel_last = np.moveaxis(channel_first, 0, -1)

    first_result = StarDistSegmenter._prepare_image(
        channel_first,
        "2D_versatile_fluo",
        1,
        "first",
    )
    last_result = StarDistSegmenter._prepare_image(
        channel_last,
        "2D_versatile_fluo",
        1,
        "last",
    )

    assert first_result.shape == (5, 7)
    assert np.array_equal(first_result, last_result)
    assert np.all(first_result == 3.0)


def test_stardist_validates_he_channels_and_grayscale_selection() -> None:
    rgba = np.zeros((4, 5, 7), dtype=np.uint8)
    prepared = StarDistSegmenter._prepare_image(
        rgba,
        "2D_versatile_he",
        0,
        "first",
    )
    assert prepared.shape == (5, 7, 3)

    with pytest.raises(ValueError, match="requires an RGB image"):
        StarDistSegmenter._prepare_image(
            np.zeros((5, 7)),
            "2D_versatile_he",
            0,
            "last",
        )
    with pytest.raises(ValueError, match="requires channel=0"):
        StarDistSegmenter._prepare_image(
            np.zeros((5, 7)),
            "2D_versatile_fluo",
            1,
            "last",
        )


def test_otsu_threshold_segment_uses_global_otsu_threshold(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "otsu.tif"
    image = np.full((8, 8), 10, dtype=np.uint8)
    image[1:3, 1:3] = 200
    image[5:7, 5:7] = 200
    iio.imwrite(image_path, image)

    result = OtsuThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, above=True)
    )

    labels = iio.imread(labels_path)
    assert result.threshold == pytest.approx(float(threshold_otsu(image)))
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_local_threshold_segment_uses_sauvola_threshold(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "local.tif"
    image = np.full((9, 9), 20, dtype=np.uint8)
    image[2:4, 2:4] = 180
    image[5:7, 5:7] = 180
    iio.imwrite(image_path, image)

    result = LocalThresholdSegment().process_row(
        Arguments(
            input_image=image_path,
            labels=labels_path,
            block_size=3,
            k=0.2,
            offset=-20.0,
            above=True,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_watershed_segment_uses_markers_to_split_foreground(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    markers_path = tmp_path / "markers.tif"
    labels_path = tmp_path / "labels.tif"

    image = np.zeros((7, 7), dtype=np.uint8)
    image[1:6, 1:6] = 10
    markers = np.zeros((7, 7), dtype=np.uint16)
    markers[2, 2] = 7
    markers[4, 4] = 11
    iio.imwrite(image_path, image)
    iio.imwrite(markers_path, markers)

    result = WatershedSegment().process_row(
        Arguments(
            input_image=image_path,
            markers_image=markers_path,
            labels=labels_path,
            threshold=5.0,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert labels[2, 2] == 7
    assert labels[4, 4] == 11
    assert labels[0, 0] == 0
    assert set(np.unique(labels)) == {0, 7, 11}


def test_distance_watershed_segment_splits_touching_binary_objects(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "mask.tif"
    labels_path = tmp_path / "distance.tif"
    yy, xx = np.ogrid[:11, :13]
    mask = ((yy - 5) ** 2 + (xx - 4) ** 2 <= 9) | (
        (yy - 5) ** 2 + (xx - 8) ** 2 <= 9
    )
    iio.imwrite(image_path, mask.astype(np.uint8))

    result = DistanceWatershedSegment().process_row(
        Arguments(
            input_image=image_path,
            labels=labels_path,
            threshold=0.5,
            min_distance=3,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert labels[5, 4] != 0
    assert labels[5, 8] != 0
    assert labels[5, 4] != labels[5, 8]


def test_split_touching_objects_splits_each_clumped_label(tmp_path: Path) -> None:
    input_path = tmp_path / "clump.tif"
    output_path = tmp_path / "split.tif"
    yy, xx = np.ogrid[:11, :13]
    labels_in = np.zeros((11, 13), dtype=np.uint16)
    labels_in[
        ((yy - 5) ** 2 + (xx - 4) ** 2 <= 9)
        | ((yy - 5) ** 2 + (xx - 8) ** 2 <= 9)
    ] = 5
    iio.imwrite(input_path, labels_in)

    result = SplitTouchingObjects().process_row(
        Arguments(labels=input_path, output_labels=output_path, min_distance=3)
    )

    labels = iio.imread(output_path)
    assert result.object_count == 2
    assert labels[5, 4] != labels[5, 8]
    assert set(np.unique(labels)) == {0, 1, 2}


def test_split_touching_objects_handles_sparse_uint32_ids(tmp_path: Path) -> None:
    input_path = tmp_path / "sparse_clumps.tif"
    output_path = tmp_path / "split.tif"
    labels_in = np.zeros((5, 5), dtype=np.uint32)
    labels_in[1:4, 1:4] = np.iinfo(np.uint32).max
    iio.imwrite(input_path, labels_in)

    result = SplitTouchingObjects().process_row(
        Arguments(labels=input_path, output_labels=output_path, min_distance=5)
    )

    labels = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(labels)) == {0, 1}


def test_filter_labels_removes_by_area_border_and_intensity(tmp_path: Path) -> None:
    input_path = tmp_path / "labels.tif"
    intensity_path = tmp_path / "intensity.tif"
    output_path = tmp_path / "filtered.tif"
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[1:4, 1:4] = 5
    labels[0:3, 5:7] = 7
    labels[6, 6] = 9
    intensity = np.zeros((8, 8), dtype=np.float32)
    intensity[labels == 5] = 10.0
    intensity[labels == 7] = 10.0
    intensity[labels == 9] = 2.0
    iio.imwrite(input_path, labels)
    iio.imwrite(intensity_path, intensity)

    result = FilterLabels().process_row(
        Arguments(
            labels=input_path,
            output_labels=output_path,
            min_area=4,
            max_area=20,
            remove_border_touching=True,
            intensity_image=intensity_path,
            min_mean_intensity=5.0,
            min_solidity=0.0,
            max_eccentricity=1.0,
        )
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[2, 2] == 1
    assert output[1, 5] == 0
    assert output[6, 6] == 0


def test_filter_labels_handles_sparse_uint32_ids(tmp_path: Path) -> None:
    input_path = tmp_path / "sparse_labels.tif"
    output_path = tmp_path / "filtered.tif"
    labels = np.zeros((5, 5), dtype=np.uint32)
    labels[1:4, 1:4] = np.iinfo(np.uint32).max
    iio.imwrite(input_path, labels)

    result = FilterLabels().process_row(
        Arguments(
            labels=input_path,
            output_labels=output_path,
            min_area=1,
            max_area=0,
            remove_border_touching=False,
            intensity_image="",
            min_mean_intensity=0.0,
            min_solidity=0.0,
            max_eccentricity=1.0,
        )
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}


def test_filter_labels_handles_volumetric_labels_with_default_shape_filters(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "labels_3d.tif"
    output_path = tmp_path / "filtered_3d.tif"
    labels = np.zeros((4, 8, 8), dtype=np.uint16)
    labels[1:3, 2:5, 2:5] = 5
    labels[:, 0:2, 0:2] = 7
    iio.imwrite(input_path, labels, photometric="minisblack")

    result = FilterLabels().process_row(
        Arguments(
            labels=input_path,
            output_labels=output_path,
            min_area=8,
            max_area=0,
            remove_border_touching=True,
            intensity_image="",
            min_mean_intensity=0.0,
            min_solidity=0.0,
            max_eccentricity=1.0,
        )
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[1, 3, 3] == 1
    assert output[1, 0, 0] == 0


def test_filter_labels_rejects_planar_shape_filters_for_volume(tmp_path: Path) -> None:
    input_path = tmp_path / "labels_3d.tif"
    labels = np.zeros((3, 4, 4), dtype=np.uint16)
    labels[1, 1:3, 1:3] = 1
    iio.imwrite(input_path, labels, photometric="minisblack")

    with pytest.raises(ValueError, match="only defined for planar labels"):
        FilterLabels().process_row(
            Arguments(
                labels=input_path,
                output_labels=tmp_path / "filtered.tif",
                min_area=0,
                max_area=0,
                remove_border_touching=False,
                intensity_image="",
                min_mean_intensity=0.0,
                min_solidity=0.5,
                max_eccentricity=1.0,
            )
        )


def test_postprocess_labels_removes_small_objects_and_relabels(tmp_path: Path) -> None:
    input_path = tmp_path / "labels.tif"
    output_path = tmp_path / "post.tif"
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[1:4, 1:4] = 5
    labels[6, 6] = 9
    iio.imwrite(input_path, labels)

    result = PostprocessLabels().process_row(
        Arguments(labels=input_path, output_labels=output_path, min_size=4)
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[2, 2] == 1
    assert output[6, 6] == 0


def test_postprocess_labels_rejects_fractional_label_ids(tmp_path: Path) -> None:
    input_path = tmp_path / "fractional.tif"
    iio.imwrite(input_path, np.array([[0.0, 1.5]], dtype=np.float32))

    with pytest.raises(ValueError, match="integer label IDs"):
        PostprocessLabels().process_row(
            Arguments(
                labels=input_path,
                output_labels=tmp_path / "output.tif",
                min_size=0,
            )
        )


def test_heavy_segmentation_tools_build_graph_without_model_dependencies(
    tmp_path: Path,
) -> None:
    with Workflow(engine="direct", storage_path=tmp_path / "results") as wf:
        Cellpose3()(input_image=tmp_path / "image.tif", name="cellpose")
        CellposeSAM()(input_image=tmp_path / "image.tif", name="cellpose_sam")
        StarDistSegmenter()(input_image=tmp_path / "image.tif", name="stardist")

        assert isinstance(wf.nodes["cellpose"].tool, Cellpose3)
        assert isinstance(wf.nodes["cellpose_sam"].tool, CellposeSAM)
        assert isinstance(wf.nodes["stardist"].tool, StarDistSegmenter)
        assert (
            wf.get_environment(Cellpose3.environment).name == Cellpose3.environment.name
        )
        assert (
            wf.get_environment(CellposeSAM.environment).name
            == CellposeSAM.environment.name
        )
        assert (
            wf.get_environment(StarDistSegmenter.environment).name
            == StarDistSegmenter.environment.name
        )


@pytest.mark.model_runtime
@pytest.mark.complete
@pytest.mark.wetlands
def test_cellpose3_runtime_segments_tiny_synthetic_image(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    image_path = tmp_path / "cellpose_input.tif"
    image = np.zeros((32, 32), dtype=np.float32)
    image[10:22, 10:22] = 1.0
    iio.imwrite(image_path, image)

    with Workflow(
        storage_path=tmp_path / "results", engine="wetlands",
        wetlands_config=complete_wetlands_config,
    ) as wf:
        cellpose = Cellpose3()(
            input_image=image_path,
            diameter=12.0,
            model_type="cyto3",
            channel=0,
            nuclear_channel=0,
            channel_axis="last",
            flow_threshold=0.4,
            cellprob_threshold=-6.0,
            name="cellpose3_runtime",
        )
        result = wf.compute(cellpose)

    mask_path = Path(result.iloc[0]["mask"])
    labels = iio.imread(mask_path)
    assert labels.shape == image.shape
    assert int(result.iloc[0]["cell_count"]) == int(np.count_nonzero(np.unique(labels)))


@pytest.mark.model_runtime
@pytest.mark.complete
@pytest.mark.wetlands
def test_cellpose_sam_runtime_segments_tiny_synthetic_image(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    image_path = tmp_path / "cellpose_sam_input.tif"
    image = np.zeros((32, 32), dtype=np.float32)
    image[10:22, 10:22] = 1.0
    iio.imwrite(image_path, image)

    with Workflow(
        storage_path=tmp_path / "results",
        engine="wetlands",
        wetlands_config=complete_wetlands_config,
    ) as wf:
        cellpose_sam = CellposeSAM()(
            input_image=image_path,
            pretrained_model="cpsam_v2",
            diameter=12.0,
            channel_axis="last",
            flow_threshold=0.4,
            cellprob_threshold=-6.0,
            name="cellpose_sam_runtime",
        )
        result = wf.compute(cellpose_sam)

    mask_path = Path(result.iloc[0]["mask"])
    labels = iio.imread(mask_path)
    assert labels.shape == image.shape
    assert int(result.iloc[0]["cell_count"]) == int(
        np.count_nonzero(np.unique(labels))
    )


@pytest.mark.model_runtime
@pytest.mark.complete
@pytest.mark.wetlands
def test_stardist_runtime_segments_tiny_synthetic_image(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    image_path = tmp_path / "stardist_input.tif"
    yy, xx = np.ogrid[:48, :48]
    image = (((yy - 24) ** 2 + (xx - 24) ** 2) <= 64).astype(np.float32)
    iio.imwrite(image_path, image)

    with Workflow(
        storage_path=tmp_path / "results", engine="wetlands",
        wetlands_config=complete_wetlands_config,
    ) as wf:
        stardist = StarDistSegmenter()(
            input_image=image_path,
            model_name="2D_versatile_fluo",
            channel=0,
            channel_axis="last",
            prob_thresh=0.1,
            nms_thresh=0.4,
            normalize_low=1.0,
            normalize_high=99.8,
            name="stardist_runtime",
        )
        result = wf.compute(stardist)

    mask_path = Path(result.iloc[0]["mask"])
    labels = iio.imread(mask_path)
    assert labels.shape == image.shape
    assert int(result.iloc[0]["object_count"]) == int(
        np.count_nonzero(np.unique(labels))
    )
