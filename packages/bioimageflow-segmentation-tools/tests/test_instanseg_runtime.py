"""Optional real InstanSeg runtime acceptance test."""

import json
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import InstanSegSegment


pytestmark = [pytest.mark.package_tools, pytest.mark.complete]


def test_real_instanseg_named_model_inference(tmp_path: Path) -> None:
    pytest.importorskip("instanseg")
    image = np.zeros((96, 96), dtype=np.uint16)
    yy, xx = np.ogrid[:96, :96]
    image[(yy - 32) ** 2 + (xx - 32) ** 2 < 11**2] = 50000
    image[(yy - 65) ** 2 + (xx - 63) ** 2 < 13**2] = 60000
    source = tmp_path / "nuclei.tif"
    iio.imwrite(source, image)
    mask = tmp_path / "mask.tif"
    provenance = tmp_path / "provenance.json"

    result = InstanSegSegment().process_row(
        Arguments(
            input_image=source,
            model_name="fluorescence_nuclei_and_cells",
            model_path=None,
            target="nuclei",
            pixel_size_um=0.5,
            channel_axis="first",
            channel_ids=None,
            processing_method="small",
            device="cpu",
            mask=mask,
            model_provenance=provenance,
        )
    )

    labels = iio.imread(mask)
    details = json.loads(provenance.read_text())
    assert labels.shape == image.shape
    assert labels.dtype == np.uint32
    assert result.object_count == len(np.unique(labels[labels > 0]))
    assert details["model_version"] == "0.1.1"
    assert len(details["model_sha256"]) == 64
