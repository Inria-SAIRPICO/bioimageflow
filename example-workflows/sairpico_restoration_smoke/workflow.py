"""SAIRPICO restoration/deconvolution smoke workflow.

This workflow intentionally uses tiny synthetic input. Tests monkeypatch the
SAIRPICO subprocess calls to validate command construction without requiring
the real SAIRPICO binaries in the default environment.
"""

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_common_tools import Collect
from bioimageflow_sairpico_tools import MedianDenoising, RichardsonLucyDeconvolution


def _write_synthetic_input(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:32, 0:32]
    image = np.exp(-(((yy - 16) ** 2 + (xx - 16) ** 2) / 40.0)).astype(np.float32)
    image_path = data_dir / "synthetic_sairpico_input.tif"
    iio.imwrite(image_path, image)
    return image_path


def build_workflow(
    storage_path: str = "./sairpico_restoration_smoke_results",
    use_wetlands: bool = False,
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build a SAIRPICO denoise plus deconvolution smoke graph."""
    storage = Path(storage_path)
    image_path = _write_synthetic_input(storage / "data")

    wf = Workflow(
        storage_path=str(storage / "bif"),
        use_wetlands=use_wetlands,
        wetlands_config=wetlands_config,
    )
    with wf:
        denoised = MedianDenoising()(
            input_image=image_path,
            denoising_type="2D",
            radius_x=1,
            radius_y=1,
            padding=True,
            name="median_denoise_2d",
        )
        deconvolved = RichardsonLucyDeconvolution()(
            input_image=denoised["output_image"],
            deconvolution_type="2D",
            sigma=1.2,
            niter=3,
            regularization_lambda=0.01,
            padding=True,
            name="richardson_lucy_2d",
        )
        outputs = Collect()(denoised, deconvolved, name="collect_sairpico_outputs")
    return wf, outputs


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
