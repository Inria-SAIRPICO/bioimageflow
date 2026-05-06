"""
FISH Image Analysis Workflow — using Sub-Workflows
===================================================

This is a refactored version of the fish_analysis workflow that uses
SubWorkflows to eliminate duplication. The original workflow repeats the
same three-step pattern (ExtractChannel → Atlas → ConnectedComponents)
for both the FOLS2 and CSF1R channels. Here, that pattern is packaged
as a reusable ``SpotDetection`` sub-workflow.

We go one step further: ``SpotAnalysis`` is a *nested* sub-workflow
(sub-sub-workflow) that wraps SpotDetection + LabelOverlaps, because
that combination is also repeated twice.

Pipeline topology (sub-workflows shown as boxes)::

  DownloadImages → ConvertImage ─┬────────────────────────────────────────┐
                                 │                                        │
                                 ├── ┌─SpotAnalysis(ch0, FOLS2)─────────┐ │
                                 │   │ SpotDetection → LabelOverlaps    │ │
                                 │   └──────────────────────────────────-┘ │
                                 │                                        │
                                 ├── ┌─SpotAnalysis(ch1, CSF1R)─────────┐ │
                                 │   │ SpotDetection → LabelOverlaps    │ │
                                 │   └──────────────────────────────────-┘ │
                                 │                                        │
                                 └── CellposeSAM (ch2, nuclei) ──────────┘
                                                                   │
                              AverageSpotsPerNucleus(FOLS2 overlaps, CSF1R overlaps)

Compare with the original ``fish_analysis/workflow.py`` — the sub-workflow
version has the same pipeline topology but less repetition and clearer intent.

Data source: Cell Image Library (CIL)
  - https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
  - https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
  - https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
  - https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif
"""

import sys
from pathlib import Path

from bioimageflow import Workflow, configure_wetlands
from bioimageflow.engine import SequentialEngine
from bioimageflow.node import Node

from bioimageflow_common_tools import CellposeSAM, ConvertImage

# Workflow-specific tools (shared with the original fish_analysis)
_fish_dir = str(Path(__file__).resolve().parent.parent / "fish_analysis")
if _fish_dir not in sys.path:
    sys.path.insert(0, _fish_dir)

from tools.average_spots_per_nucleus import AverageSpotsPerNucleus  # noqa: E402
from tools.download_images import DownloadImages  # noqa: E402

# Sub-workflows defined in this package
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from sub_workflows import SpotAnalysis  # noqa: E402

CIL_URLS = """\
https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif
https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif
https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif
https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif"""


def build_fish_workflow(
    storage_path: str = "./fish_sub_results",
    data_dir: str = "./data",
    debug: bool = False,
) -> "tuple[Workflow, Node]":
    """Build the FISH analysis workflow using sub-workflows.

    Parameters
    ----------
    storage_path : str
        Where to store intermediate and cached results.
    data_dir : str
        Where to download/store the raw CIL images.
    debug : bool
        Enable Wetlands debug mode.

    Returns
    -------
    tuple of (Workflow, Node)
        The workflow and the terminal stats node.
    """
    wf = Workflow(
        storage_path=storage_path,
        wetlands_config={"debug": debug},
    )

    # Build the graph inside the context manager so nodes auto-register.
    # All variables assigned here are guaranteed bound when `with` exits
    # normally (no early return/break).
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

        # -- 3. Nuclei segmentation (channel 2) --
        nuclei = CellposeSAM()(
            input_image=converted["output_image"],
            model_type="nuclei",
            name="cellpose_nuclei",
        )

        # -- 4. Spot analysis for each channel --
        #
        # This is where sub-workflows shine: instead of repeating
        # ExtractChannel → Atlas → ConnectedComponents → LabelOverlaps
        # for each channel, we call SpotAnalysis twice with different params.
        #
        # SpotAnalysis is a *nested* sub-workflow: it contains SpotDetection
        # internally, which itself is a sub-workflow.

        fols2 = SpotAnalysis()(
            input_image=converted["output_image"],
            reference_image=nuclei["mask"],
            channel=0,
            name="fols2_analysis",
        )

        csfr1 = SpotAnalysis()(
            input_image=converted["output_image"],
            reference_image=nuclei["mask"],
            channel=1,
            name="csfr1_analysis",
        )

        # -- 5. Statistical aggregation --
        stats = AverageSpotsPerNucleus()(
            fols2, csfr1,
            name="avg_spots_per_nucleus",
        )

    return wf, stats  # type: ignore[possibly-undefined]


def main() -> None:
    storage_path = sys.argv[1] if len(sys.argv) > 1 else "./fish_sub_results"
    data_dir = sys.argv[2] if len(sys.argv) > 2 else "./data"

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf, stats = build_fish_workflow(storage_path, data_dir, debug=True)

    # Step-by-step execution — shows internal sub-workflow nodes with
    # scoped names like "fols2_analysis/spot_detection_1/extract_channel_1"
    engine = SequentialEngine(
        use_wetlands=wf.use_wetlands,
        wetlands_config=wf.wetlands_config,
    )

    for step in wf.compute_steps(engine=engine):
        env_name = step.environment.name if step.environment else "main"
        print(f"  {step.node_name}  [{env_name}]", flush=True)
        df = step.execute()
        print(f"    -> {len(df)} rows, columns: {list(df.columns)}", flush=True)

    print("\nWorkflow complete.", flush=True)


if __name__ == "__main__":
    main()
