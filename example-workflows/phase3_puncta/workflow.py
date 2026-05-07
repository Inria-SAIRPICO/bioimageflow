"""Synthetic puncta detection, assignment, and summary workflow."""

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow_spot_tools import AssignSpotsToLabels, DetectSpots, SpotSummary


def _write_inputs(data_dir: Path) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    image = np.zeros((64, 64), dtype=np.float32)
    for y, x in [(16, 18), (22, 44), (46, 32)]:
        image[y, x] = 10.0
        image[y - 1 : y + 2, x - 1 : x + 2] += 2.5
    labels = np.zeros((64, 64), dtype=np.uint16)
    labels[:32, :] = 1
    labels[32:, :] = 2
    image_path = data_dir / "synthetic_puncta.tif"
    labels_path = data_dir / "synthetic_labels.tif"
    iio.imwrite(image_path, image)
    iio.imwrite(labels_path, labels)
    return image_path, labels_path


def build_workflow(storage_path: str = "./phase3_puncta_results") -> tuple[Workflow, object]:
    """Build the Phase 3 puncta workflow."""
    storage = Path(storage_path)
    image_path, labels_path = _write_inputs(storage / "data")
    wf = Workflow(storage_path=str(storage / "bif"))
    with wf:
        detected = DetectSpots()(
            input_image=image_path,
            method="dog",
            threshold=0.3,
            min_distance=5,
            name="detect_puncta",
        )
        assigned = AssignSpotsToLabels()(
            spots_csv=detected["spots_csv"],
            label_image=labels_path,
            name="assign_to_labels",
        )
        summary = SpotSummary()(
            assigned_spots_csv=assigned["assigned_spots_csv"],
            name="summarize_puncta",
        )
    return wf, summary


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
