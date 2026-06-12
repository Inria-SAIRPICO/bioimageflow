# bioimageflow-spot-tools

`bioimageflow-spot-tools` provides a focused puncta workflow: detect spots,
assign spot coordinates to label images, and summarize spot counts/intensities
per object. It is intended for FISH-like, smFISH-like, synapse, vesicle, or
small puncta examples where a lightweight and deterministic baseline is enough.

Core libraries are imageio, NumPy, and BioImageFlow core APIs.
SciPy is used opportunistically for LoG scoring when available; a NumPy fallback keeps default tests runnable.
Big-FISH remains a future optional evaluation backend, not a current default dependency.

## Tools

- [DetectSpots](#detectspots): LoG, DoG, or local-maxima spot
  detection.
- [AssignSpotsToLabels](#assignspotstolabels): sample label values
  at spot coordinates.
- [SpotSummary](#spotsummary): aggregate assigned spot counts and
  intensities per label.
- [FilterSpots](#filterspots): filter spot dataframe rows by numeric thresholds and masks.
- [RenderSpots](#renderspots): render coordinates to label or mask images.
- [SpotsToLabels](#spotstolabels): create spot label images from dataframe rows or masks.
- [SpotColocalization](#spotcolocalization): match two upstream spot dataframes by distance.
- [SpotQualityMetrics](#spotqualitymetrics): compute SNR, local background,
  and nearest-neighbor distances.

## Demo Workflow

- [Puncta analysis workflow](#puncta-analysis-workflow): detect puncta,
  assign them to labels, and summarize per-object signal.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-spot-tools/tests
```

Tests generate a tiny puncta image and a label mask, then assert exact spot counts, dataframe columns, assignments, and workflow graph execution.
