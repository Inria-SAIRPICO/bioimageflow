"""OME-TIFF and OME-Zarr normalization workflow using bioimageflow-io-tools."""

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow_common_tools import Collect
from bioimageflow_io_tools import (
    ConvertToOmeTiff,
    ConvertToOmeZarr,
    ReadImage,
    SelectDimensions,
)


def _write_synthetic_input(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    data = np.arange(2 * 3 * 16 * 18, dtype=np.uint16).reshape(2, 3, 16, 18)
    source = data_dir / "synthetic_czyx.tif"
    iio.imwrite(source, data, photometric="minisblack")
    return source


def build_workflow(
    storage_path: str = "./ome_normalization_results",
) -> tuple[Workflow, object]:
    """Build a tiny CZYX to selected OME-TIFF/OME-Zarr conversion graph."""
    storage = Path(storage_path)
    source = _write_synthetic_input(storage / "data")

    wf = Workflow(storage_path=str(storage / "bif"), use_wetlands=False)
    with wf:
        read = ReadImage()(input_image=source, name="read_source")
        selected = SelectDimensions()(
            input_image=read["output_image"],
            layout="CZYX",
            channel=1,
            z=2,
            name="select_channel_z",
        )
        ome_tiff = ConvertToOmeTiff()(
            input_image=selected["output_image"],
            dimension_order="YX",
            name="convert_to_ome_tiff",
        )
        ome_zarr = ConvertToOmeZarr()(
            input_image=selected["output_image"],
            name="convert_to_ome_zarr",
        )
        outputs = Collect()(ome_tiff, ome_zarr, name="collect_normalized_outputs")
    return wf, outputs


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
