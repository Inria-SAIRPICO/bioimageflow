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

  DownloadImages ─┬─ SelectChannel(ch0) → AtlasSpotDetection → ConnectedComponents ─┐
                  ├─ SelectChannel(ch1) → AtlasSpotDetection → ConnectedComponents ─┤
                  └─ SelectChannel(ch2) → Cellpose3 ────────────────────────────────┤
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

from bioimageflow_io_tools import SelectChannel
from bioimageflow_segmentation_tools import Cellpose3
from tools.average_spots_per_nucleus import AverageSpotsPerNucleus
from tools.download_images import DownloadImages
from tools.marker_spot_analysis import build_workflow as build_marker_workflow

CIL_URLS = """\
https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif"""

DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    debug: bool = False,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build and return a fresh FISH analysis workflow.

    Parameters
    ----------
    storage_path : str
        Where to store intermediate and cached results.
    """
    wf = Workflow(
        name="fish_analysis",
        display_name="Fish Analysis",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config={**(wetlands_config or {}), "debug": debug},
    )

    with wf:
        # -- 1. Data ingestion --
        download = DownloadImages()(
            urls=CIL_URLS,
            name="download_cil_images",
        )

        # -- 2. Nuclei channel extraction before Cellpose v3 --
        ch_nuclei = SelectChannel()(
            input_image=download["path"],
            layout="CYX",
            channel=2,
            name="extract_ch2_nuclei",
        )
        nuclei = Cellpose3()(
            input_image=ch_nuclei["output_image"],
            model_type="nuclei",
            name="cellpose3_nuclei",
        )

        # -- 4. Marker spot analysis branches --
        marker_workflow = build_marker_workflow(storage_path=storage_path)
        overlaps_fols2 = marker_workflow(
            input_image=download["path"],
            nuclei_labels=nuclei["mask"],
            channel=0,
            name="fols2_marker_spot_analysis",
        )
        overlaps_csf1r = marker_workflow(
            input_image=download["path"],
            nuclei_labels=nuclei["mask"],
            channel=1,
            name="csf1r_marker_spot_analysis",
        )

        # -- 5. Statistical aggregation --
        stats = AverageSpotsPerNucleus()(
            overlaps_fols2,
            overlaps_csf1r,
            name="avg_spots_per_nucleus",
        )
        wf.output("image_index", stats["image_index"], id="output-image-index")
        wf.output(
            "avg_fols2_per_nucleus", stats["avg_fols2_per_nucleus"], id="output-fols2"
        )
        wf.output(
            "avg_csf1r_per_nucleus", stats["avg_csf1r_per_nucleus"], id="output-csf1r"
        )
    return wf


def main() -> None:
    storage_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STORAGE_PATH

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf = build_workflow(storage_path=storage_path, debug=True)

    # Create an explicit engine for debugging and inspection
    engine = SequentialEngine(
        use_wetlands=wf.engine_type == "wetlands",
        wetlands_config=wf.wetlands_config,
    )

    for step in wf.compute_steps(
        engine=engine,
    ):
        print(f"Next: {step.node_name} (env: {step.environment})")
        step.prepare()  # launches Wetlands env — attach debugger here
        df = step.execute()  # runs the tool
        print(df.head())

    # After execution, the engine can be inspected for internal state
    # For example: print(engine._env_manager) if wetlands were used
    # result = wf.compute()

    # print("\n=== FISH Analysis Results ===")
    # print(result.to_string(index=False))
    # print()


if __name__ == "__main__":
    main()
