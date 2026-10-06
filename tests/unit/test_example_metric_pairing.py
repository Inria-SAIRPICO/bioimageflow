"""Example metrics associate scientific values by actual row keys."""

from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow_core import Arguments
from example_workflows.parameter_space_exploration.parameter_tools.metrics import (
    ParameterSweepResults,
)
from example_workflows.sairpico_deconvolution.workflow import DeconvolutionMetrics


def _parameter_tables() -> list[pd.DataFrame]:
    return [
        pd.DataFrame({"sensitivity": [1.0, 9.0], "size": [1, 9]}, index=["A", "B"]),
        pd.DataFrame({"output_image": ["B-mask", "A-mask"]}, index=["B", "A"]),
        pd.DataFrame({"label_count": [9, 1]}, index=["B", "A"]),
        pd.DataFrame({"mosaic_path": ["mosaic"], "image_count": [2]}),
    ]


def test_parameter_metrics_align_keyed_values_and_keep_mosaic_aggregate() -> None:
    result = ParameterSweepResults().merge_dataframes(_parameter_tables(), Arguments())
    assert result.index.tolist() == ["A", "B"]
    assert result["sensitivity"].tolist() == [1.0, 9.0]
    assert result["output_image"].tolist() == ["A-mask", "B-mask"]
    assert result["label_count"].tolist() == [1, 9]
    assert result["mosaic_path"].tolist() == ["mosaic", "mosaic"]
    assert result["image_count"].tolist() == [2, 2]


@pytest.mark.parametrize("labels", [["A"], ["A", "B", "C"], ["A", "A"]])
def test_parameter_metrics_refuse_incomplete_or_ambiguous_keys(labels: list[str]) -> None:
    tables = _parameter_tables()
    tables[1] = pd.DataFrame({"output_image": ["mask"] * len(labels)}, index=labels)
    with pytest.raises(ValueError):
        ParameterSweepResults().merge_dataframes(tables, Arguments())


def test_deconvolution_metrics_pair_actual_image_values_by_keys(tmp_path: Path) -> None:
    paths = []
    for value in (1, 9):
        path = tmp_path / f"image-{value}.tif"
        iio.imwrite(path, np.array([[0, value], [0, value]], dtype=np.float32))
        paths.append(path)
    first = pd.DataFrame(
        {"output_image_left": [str(paths[0])] * 2,
         "output_image_right": [str(paths[0])] * 2}, index=["A", "B"],
    )
    second = pd.DataFrame({"output_image": [str(paths[1]), str(paths[0])]}, index=["B", "A"])

    result = DeconvolutionMetrics().merge_dataframes(
        [first, second], Arguments(input_image=paths[0]),
    )

    assert result.index.tolist() == ["A", "B"]
    assert result["deconvolved_image"].tolist() == [str(paths[0]), str(paths[1])]
    assert result["deconvolved_sharpness"].tolist() == [1.0, 81.0]
    assert result["denoised_residual_noise"].tolist() == [0.0, 0.0]


@pytest.mark.parametrize("labels", [["A"], ["A", "B", "C"], ["A", "A"]])
def test_deconvolution_pairing_refuses_before_image_io(
    labels: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[object] = []
    monkeypatch.setattr(iio, "imread", lambda path: reads.append(path))
    first = pd.DataFrame(
        {"output_image_left": ["psf"] * 2, "output_image_right": ["denoised"] * 2},
        index=["A", "B"],
    )
    second = pd.DataFrame({"output_image": ["deconvolved"] * len(labels)}, index=labels)
    with pytest.raises(ValueError):
        DeconvolutionMetrics().merge_dataframes([first, second], Arguments(input_image="input"))
    assert reads == []
