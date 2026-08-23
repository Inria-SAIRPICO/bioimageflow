"""PTU phasor calibration, filtering, and apparent-lifetime workflow."""

import argparse
from pathlib import Path

from bioimageflow import Workflow
from bioimageflow_phasor_tools import (
    CalibratePhasor,
    FilterPhasor,
    PhasorToApparentLifetime,
    PtuToPhasor,
)


DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build the calibrated PTU phasor workflow."""
    wf = Workflow(
        name="flim_phasor_analysis",
        display_name="FLIM Phasor Analysis",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        sample_ptu = wf.input("sample_ptu", Path, id="input-sample-ptu")
        reference_ptu = wf.input("reference_ptu", Path, id="input-reference-ptu")
        reference_lifetime = wf.input(
            "reference_lifetime_ns",
            float,
            default=4.2,
            id="input-reference-lifetime",
        )
        sample = PtuToPhasor()(ptu_file=sample_ptu, name="sample_ptu_to_phasor")
        reference = PtuToPhasor()(ptu_file=reference_ptu, name="reference_ptu_to_phasor")
        calibrated = CalibratePhasor()(
            phasor_ome_tiff=sample["phasor_ome_tiff"],
            reference_ome_tiff=reference["phasor_ome_tiff"],
            reference_lifetime_ns=reference_lifetime,
            name="calibrate_sample_phasor",
        )
        filtered = FilterPhasor()(
            phasor_ome_tiff=calibrated["calibrated_ome_tiff"],
            median_size=3,
            median_repeat=2,
            mean_min=1.0,
            name="filter_sample_phasor",
        )
        lifetime = PhasorToApparentLifetime()(
            phasor_ome_tiff=filtered["filtered_ome_tiff"],
            name="apparent_lifetimes",
        )
        wf.output("sample_phasor", sample["phasor_ome_tiff"], id="output-sample-phasor")
        wf.output("calibrated_phasor", calibrated["calibrated_ome_tiff"], id="output-calibrated")
        wf.output("filtered_phasor", filtered["filtered_ome_tiff"], id="output-filtered")
        wf.output("phase_lifetime", lifetime["phase_lifetime"], id="output-phase-lifetime")
        wf.output(
            "modulation_lifetime",
            lifetime["modulation_lifetime"],
            id="output-modulation-lifetime",
        )
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-ptu", required=True)
    parser.add_argument("--reference-ptu", required=True)
    parser.add_argument("--reference-lifetime-ns", type=float, default=4.2)
    parser.add_argument("--storage-path", default=str(DEFAULT_STORAGE_PATH))
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(
        workflow.compute(
            inputs={
                "sample_ptu": args.sample_ptu,
                "reference_ptu": args.reference_ptu,
                "reference_lifetime_ns": args.reference_lifetime_ns,
            }
        ).to_string(index=False)
    )
