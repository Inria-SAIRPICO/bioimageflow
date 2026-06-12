"""Complete Wetlands portability tests for common tools."""

from __future__ import annotations

from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow import Workflow
from bioimageflow_common_tools import ConnectedComponents

pytestmark = [
    pytest.mark.package_tools,
    pytest.mark.complete,
    pytest.mark.wetlands,
]


def test_connected_components_labels_binary_objects_through_wetlands(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    image = np.zeros((12, 12), dtype=np.uint8)
    image[1:4, 1:4] = 1
    image[7:10, 8:11] = 1
    input_path = tmp_path / "binary.tif"
    iio.imwrite(input_path, image)

    with Workflow(
        storage_path=tmp_path / "results",
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    ) as wf:
        labels_node = ConnectedComponents()(
            input_image=input_path,
            name="connected_components",
        )
        result = wf.compute(labels_node)

    output_path = Path(result.iloc[0]["output_image"])
    labels = iio.imread(output_path)
    assert int(result.iloc[0]["num_labels"]) == 2
    assert labels.max() == 2
    assert labels[2, 2] != 0
    assert labels[8, 9] != 0
    assert labels[2, 2] != labels[8, 9]
