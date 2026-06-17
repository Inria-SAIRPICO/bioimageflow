# bioimageflow-spot-tools

`bioimageflow-spot-tools` provides a focused puncta workflow: detect spots,
assign spot coordinates to label images, and summarize spot counts/intensities
per object. It is intended for FISH-like, smFISH-like, synapse, vesicle, or
small puncta examples where a lightweight and deterministic baseline is enough.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
SciPy is used opportunistically for LoG scoring when available; a NumPy fallback keeps default tests runnable.
Big-FISH remains a future optional evaluation backend, not a current default dependency.

## Tools

- <a href="tools/detect_spots.md">DetectSpots</a>: LoG, DoG, or local-maxima spot
  detection.
- <a href="tools/assign_spots_to_labels.md">AssignSpotsToLabels</a>: sample label values
  at spot coordinates.
- <a href="tools/spot_summary.md">SpotSummary</a>: aggregate assigned spot counts and
  intensities per label.
- <a href="tools/filter_spots.md">FilterSpots</a>: filter spot dataframe rows by numeric thresholds and masks.
- <a href="tools/render_spots.md">RenderSpots</a>: render coordinates to label or mask images.
- <a href="tools/spots_to_labels.md">SpotsToLabels</a>: create spot label images from dataframe rows or masks.
- <a href="tools/spot_colocalization.md">SpotColocalization</a>: match two upstream spot dataframes by distance.
- <a href="tools/spot_quality_metrics.md">SpotQualityMetrics</a>: compute SNR, local background,
  and nearest-neighbor distances.

## Demo Workflow

- <a href="workflows/puncta_analysis.md">Puncta analysis workflow</a>: detect puncta,
  assign them to labels, and summarize per-object signal.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-spot-tools/tests
```

Tests generate a tiny puncta image and a label mask, then assert exact spot counts, dataframe columns, assignments, and workflow graph execution.
