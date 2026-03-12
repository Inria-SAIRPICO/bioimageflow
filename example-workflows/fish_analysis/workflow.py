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

  DownloadImages → ConvertImage → ┬─ ExtractChannel(ch0) → Atlas → ConnectedComponents ─┐
                                  ├─ ExtractChannel(ch1) → Atlas → ConnectedComponents ─┤
                                  └─ ExtractChannel(ch2) → CellposeSAM ────────────────┤
                                                                                        │
                             LabelOverlaps(FOLS2 spots vs nuclei) ◄─────────────────────┤
                             LabelOverlaps(CSF1R spots vs nuclei) ◄─────────────────────┘
                                     │                    │
                                     └──── Collect ───────┘
                                             │
                                  AverageSpotsPerNucleus
"""

import sys

from bioimageflow import Workflow, Collect
from bioimageflow.engine import SequentialEngine

from bioimageflow_common_tools import (
    ConvertImage,
    ExtractChannel,
    Atlas,
    ConnectedComponents,
    CellposeSAM,
    LabelOverlaps,
)

# Workflow-specific tools
from tools import DownloadImages, AverageSpotsPerNucleus

CIL_URLS = """\
https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif"""


def build_fish_workflow(
    storage_path: str = "./fish_results",
    data_dir: str = "./data",
    debug: bool=False
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
    wf = Workflow(storage_path=storage_path, wetlands_config={"debug": debug})

    with wf:
        # -- 1. Data ingestion --
        download = DownloadImages()(
            urls=CIL_URLS,
            output_dir=data_dir,
            name="download_cil_images",
        )

        # -- 2. Preprocessing --
        converted = ConvertImage()(
            input_image=download["path"],
            name="convert_image",
        )

        # -- 3. Channel extraction (3 parallel branches) --
        ch_fols2 = ExtractChannel()(
            input_image=converted["output_image"], channel=0,
            name="extract_ch0_fols2",
        )
        ch_csfr1 = ExtractChannel()(
            input_image=converted["output_image"], channel=1,
            name="extract_ch1_csfr1",
        )
        ch_nuclei = ExtractChannel()(
            input_image=converted["output_image"], channel=2,
            name="extract_ch2_nuclei",
        )

        # -- 4. Spot detection (channels 0 & 1) --
        spots_fols2 = Atlas()(
            input_image=ch_fols2["output_image"],
            name="atlas_fols2",
        )
        spots_csfr1 = Atlas()(
            input_image=ch_csfr1["output_image"],
            name="atlas_csfr1",
        )

        labels_fols2 = ConnectedComponents()(
            input_image=spots_fols2["output_image"],
            name="cc_fols2",
        )
        labels_csfr1 = ConnectedComponents()(
            input_image=spots_csfr1["output_image"],
            name="cc_csfr1",
        )

        # -- 5. Nuclei segmentation (channel 2) --
        nuclei = CellposeSAM()(
            input_image=ch_nuclei["output_image"],
            model_type="nuclei",
            name="cellpose_nuclei",
        )

        # -- 6. Spatial correlation --
        overlaps_fols2 = LabelOverlaps()(
            label_image=labels_fols2["output_image"],
            reference_image=nuclei["mask"],
            name="overlaps_fols2",
        )
        overlaps_csfr1 = LabelOverlaps()(
            label_image=labels_csfr1["output_image"],
            reference_image=nuclei["mask"],
            name="overlaps_csfr1",
        )

        # -- 7. Statistical aggregation --
        collected = Collect()(
            overlaps_fols2, overlaps_csfr1,
            name="collect_overlaps",
        )
        stats = AverageSpotsPerNucleus()(
            collected,
            name="avg_spots_per_nucleus",
        )

    return wf, stats  # type: ignore[possibly-undefined]  # always defined in with-block


def main() -> None:
    storage_path = sys.argv[1] if len(sys.argv) > 1 else "./fish_results"
    data_dir = sys.argv[2] if len(sys.argv) > 2 else "./data"

    wf, stats = build_fish_workflow(storage_path, data_dir, debug=True)

    # Create an explicit engine for debugging and inspection
    engine = SequentialEngine(
        use_wetlands=wf.use_wetlands,
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
