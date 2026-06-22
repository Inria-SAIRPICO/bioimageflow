"""
FISH Image Analysis Workflow
============================

Quantifies the presence of FOLS2 and CSF1R gene markers within nuclei
in breast cancer tissue samples from fluorescence in situ hybridization
(FISH) microscopy images.

Data source: Cell Image Library (CIL)
  - https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
  - https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
  - https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
  - https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif

Input: three-channel microscopy images
  - Channel 0 (green): FOLS2
  - Channel 1 (red):   CSF1R
  - Channel 2 (blue):  Nuclei

Output: average number of FOLS2 and CSF1R spots per nucleus per image.

Pipeline topology:

  DownloadImages ─┬─ ExtractChannel(ch0) → AtlasSpotDetection → ConnectedComponents ─┐
                  ├─ ExtractChannel(ch1) → AtlasSpotDetection → ConnectedComponents ─┤
                  └─ ExtractChannel(ch2) → Cellpose3 ────────────────────────────────┤
                                                                                     │
                  LabelOverlaps(FOLS2 spots vs nuclei) ◄─────────────────────────────┤
                  LabelOverlaps(CSF1R spots vs nuclei) ◄─────────────────────────────┘
                          │                    │
                          └──── AverageSpotsPerNucleus
"""

import sys
from pathlib import Path

from bioimageflow import Workflow, configure_wetlands
from bioimageflow.engine import SequentialEngine
from bioimageflow.node import Node

from bioimageflow_common_tools import Collect, ExtractChannel, Files
from bioimageflow_segmentation_tools import Cellpose3
from bioimageflow_segmentation_tools import ThresholdSegment
from bioimageflow_spot_tools import (
    AssignSpotsToLabels,
    DetectSpots,
    SpotSummary,
)

# Workflow-specific tools
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from tools.download_images import DownloadImages  # noqa: E402
from tools.average_spots_per_nucleus import AverageSpotsPerNucleus  # noqa: E402
from tools.marker_spot_analysis import MarkerSpotAnalysis  # noqa: E402

CIL_URLS = """\
https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif"""


def build_fish_workflow(
    storage_path: str = "./fish_results",
    data_dir: str = "./data",
    debug: bool = False,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple:
    """Build and return the FISH analysis workflow and its terminal node.

    Parameters
    ----------
    storage_path : str
        Where to store intermediate and cached results.
    data_dir : str
        Where to download/store the raw CIL images.

    Returns
    -------
    tuple of (Workflow, Node)
        The workflow and the terminal stats node.
    """
    wf = Workflow(
        storage_path=storage_path,
        engine=engine,
        wetlands_config={**(wetlands_config or {}), "debug": debug},
    )

    with wf:
        # -- 1. Data ingestion --
        download = DownloadImages()(
            urls=CIL_URLS,
            output_dir=data_dir,
            name="download_cil_images",
        )

        # -- 2. Nuclei channel extraction before Cellpose v3 --
        ch_nuclei = ExtractChannel()(
            input_image=download["path"], channel=2,
            name="extract_ch2_nuclei",
        )
        nuclei = Cellpose3()(
            input_image=ch_nuclei["output_image"],
            model_type="nuclei",
            name="cellpose3_nuclei",
        )

        # -- 4. Marker spot analysis branches --
        overlaps_fols2 = MarkerSpotAnalysis()(
            input_image=download["path"],
            nuclei_labels=nuclei["mask"],
            marker_name="FOLS2",
            channel=0,
            name="fols2_marker_spot_analysis",
        )
        overlaps_csfr1 = MarkerSpotAnalysis()(
            input_image=download["path"],
            nuclei_labels=nuclei["mask"],
            marker_name="CSF1R",
            channel=1,
            name="csf1r_marker_spot_analysis",
        )

        # -- 5. Statistical aggregation --
        stats = AverageSpotsPerNucleus()(
            overlaps_fols2, overlaps_csfr1,
            name="avg_spots_per_nucleus",
        )

    return wf, stats  # type: ignore[possibly-undefined]  # always defined in with-block


def build_synthetic_fish_workflow(
    storage_path: str = "./fish_synthetic_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build a lightweight FISH-like workflow for tests and examples.

    This graph keeps the same high-level shape as the CIL workflow but replaces
    AtlasSpotDetection and Cellpose with small package tools that can run on a synthetic
    fixture in normal CI.
    """
    import imageio.v3 as iio
    import numpy as np

    storage = Path(storage_path)
    data_dir = storage / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    image = np.zeros((3, 48, 48), dtype=np.float32)
    nuclei = [(16, 16, 8), (32, 31, 7)]
    yy, xx = np.mgrid[0:48, 0:48]
    for cy, cx, radius in nuclei:
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        image[2, mask] = 1.0
    for channel, coordinates in {
        0: [(15, 16), (33, 30), (32, 34)],
        1: [(17, 15), (31, 31)],
    }.items():
        for y, x in coordinates:
            image[channel, y, x] = 12.0
            image[channel, y - 1 : y + 2, x - 1 : x + 2] += 2.0

    source = data_dir / "synthetic_fish_cyx.tif"
    iio.imwrite(source, image, photometric="minisblack")

    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        input_image = Files()(
            path=str(data_dir),
            pattern=source.name,
            name="synthetic_fish_input",
        )
        ch_fols2 = ExtractChannel()(
            input_image=input_image["path"],
            channel=0,
            name="extract_fols2",
        )
        ch_csf1r = ExtractChannel()(
            input_image=input_image["path"],
            channel=1,
            name="extract_csf1r",
        )
        ch_nuclei = ExtractChannel()(
            input_image=input_image["path"],
            channel=2,
            name="extract_nuclei",
        )
        nuclei_labels = ThresholdSegment()(
            input_image=ch_nuclei["output_image"],
            threshold=0.5,
            name="segment_nuclei_threshold",
        )
        fols2_spots = DetectSpots()(
            input_image=ch_fols2["output_image"],
            method="dog",
            threshold=0.2,
            min_distance=4,
            name="detect_fols2_spots",
        )
        csf1r_spots = DetectSpots()(
            input_image=ch_csf1r["output_image"],
            method="dog",
            threshold=0.2,
            min_distance=4,
            name="detect_csf1r_spots",
        )
        fols2_assigned = AssignSpotsToLabels()(
            spot_id=fols2_spots["spot_id"],
            y=fols2_spots["y"],
            x=fols2_spots["x"],
            intensity=fols2_spots["intensity"],
            score=fols2_spots["score"],
            label_image=nuclei_labels["labels"],
            name="assign_fols2_to_nuclei",
        )
        csf1r_assigned = AssignSpotsToLabels()(
            spot_id=csf1r_spots["spot_id"],
            y=csf1r_spots["y"],
            x=csf1r_spots["x"],
            intensity=csf1r_spots["intensity"],
            score=csf1r_spots["score"],
            label_image=nuclei_labels["labels"],
            name="assign_csf1r_to_nuclei",
        )
        fols2_summary = SpotSummary()(fols2_assigned, name="summarize_fols2")
        csf1r_summary = SpotSummary()(csf1r_assigned, name="summarize_csf1r")
        summary = Collect()(
            fols2_summary,
            csf1r_summary,
            name="summarize_synthetic_fish",
        )

    return wf, summary


def main() -> None:
    storage_path = sys.argv[1] if len(sys.argv) > 1 else "./fish_results"
    data_dir = sys.argv[2] if len(sys.argv) > 2 else "./data"

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf, stats = build_fish_workflow(storage_path, data_dir, debug=True)

    # Create an explicit engine for debugging and inspection
    engine = SequentialEngine(
        use_wetlands=wf.engine_type == "wetlands",
        wetlands_config=wf.wetlands_config,
    )

    for step in wf.compute_steps(engine=engine):
        print(f"Next: {step.node_name} (env: {step.environment})")
        step.prepare()     # launches Wetlands env — attach debugger here
        df = step.execute() # runs the tool
        print(df.head())

    # After execution, the engine can be inspected for internal state
    # For example: print(engine._env_manager) if wetlands were used
    # result = wf.compute(stats)

    # print("\n=== FISH Analysis Results ===")
    # print(result.to_string(index=False))
    # print()


if __name__ == "__main__":
    main()
