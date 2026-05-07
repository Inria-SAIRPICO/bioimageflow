"""Tiny BBBC038-style nuclei segmentation benchmark workflow.

The default builder writes a synthetic image/reference pair so tests can run
without downloading the public BBBC038 data. To evaluate real BBBC038 images,
replace the generated input paths with downloaded images and masks from the
Broad Bioimage Benchmark Collection.
"""

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow_measurement_tools import LabelBenchmark
from bioimageflow_segmentation_tools import ThresholdSegment


def _write_synthetic_bbbc038_fixture(data_dir: Path) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:64, 0:64]
    image = np.zeros((64, 64), dtype=np.float32)
    reference = np.zeros((64, 64), dtype=np.uint16)
    objects = [(20, 22, 8, 1), (42, 40, 10, 2)]
    for cy, cx, radius, label in objects:
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        image[mask] = 1.0
        reference[mask] = label
    image += np.linspace(0.0, 0.1, image.shape[1], dtype=np.float32)

    image_path = data_dir / "synthetic_bbbc038_image.tif"
    reference_path = data_dir / "synthetic_bbbc038_reference.tif"
    iio.imwrite(image_path, image)
    iio.imwrite(reference_path, reference)
    return image_path, reference_path


def build_workflow(
    storage_path: str = "./bbbc038_segmentation_results",
) -> tuple[Workflow, object]:
    """Build the synthetic BBBC038-style benchmark workflow."""
    storage = Path(storage_path)
    image_path, reference_path = _write_synthetic_bbbc038_fixture(storage / "data")

    wf = Workflow(storage_path=str(storage / "bif"), use_wetlands=False)
    with wf:
        predicted = ThresholdSegment()(
            input_image=image_path,
            threshold=0.5,
            name="threshold_nuclei",
        )
        benchmark = LabelBenchmark()(
            predicted_label_image=predicted["labels"],
            reference_label_image=reference_path,
            name="benchmark_against_reference",
        )
    return wf, benchmark


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
