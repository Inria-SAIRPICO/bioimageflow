# bioimageflow-spot-tools

`bioimageflow-spot-tools` provides a focused puncta workflow: detect spots,
assign spot coordinates to label images, and summarize spot counts/intensities
per object. It is intended for FISH-like, smFISH-like, synapse, vesicle, or
small puncta examples where a lightweight and deterministic baseline is enough.

Core libraries are imageio, NumPy, pandas for dataframe tools, and BioImageFlow core APIs.
SciPy is used opportunistically for LoG scoring when available; a NumPy fallback keeps default tests runnable.
Big-FISH remains a future optional evaluation backend, not a current default dependency.

## Tools

- [AtlasSpotDetection](tools/atlas_spot_detection.md): external Atlas CLI-backed
  adaptive spot detection for 2D TIFF intensity images.
- [DetectSpots](tools/detect_spots.md): LoG, DoG, or local-maxima spot
  detection.
- [AssignSpotsToLabels](tools/assign_spots_to_labels.md): sample label values
  at spot coordinates.
- [SpotSummary](tools/spot_summary.md): aggregate assigned spot counts and
  intensities per label.
- [FilterSpots](tools/filter_spots.md): filter spot dataframe rows by numeric thresholds and masks.
- [RenderSpots](tools/render_spots.md): render coordinates to label or mask images.
- [SpotsToLabels](tools/spots_to_labels.md): combine spot-coordinate rows into one label image.
- [MaskToLabels](tools/mask_to_labels.md): convert each mask row into a connected-component label image.
- [SpotColocalization](tools/spot_colocalization.md): match two upstream spot dataframes by distance.
- [SpotQualityMetrics](tools/spot_quality_metrics.md): compute SNR, local background,
  and nearest-neighbor distances.

## Workflow Use

Use `AtlasSpotDetection` in the FISH spot-counting and parameter-space exploration workflows.
Use assignment and summary tools to connect spot tables to segmented nuclei or other label images.
