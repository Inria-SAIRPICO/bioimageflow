# bioimageflow-spot-tools

Tools for puncta and spot quantification.

## Tools

- `DetectSpots`: detects local maxima after DoG/LoG-compatible filtering and returns one dataframe row per spot plus a label image.
- `AssignSpotsToLabels`: samples a label image at each detected spot coordinate.
- `SpotSummary`: aggregates spot counts and intensities per label.
- `FilterSpots`: filters spot dataframe rows by intensity, score, radius, and mask.
- `RenderSpots`: renders coordinate rows to binary mask or label images.
- `SpotsToLabels`: converts spot coordinates or masks into label images.
- `SpotColocalization`: matches two upstream spot dataframes between channels by distance.
- `SpotQualityMetrics`: computes SNR, local background, and nearest-neighbor distances.

Big-FISH is optional evaluation-only support and is not a normal package dependency. The default tools and tests use lightweight NumPy/imageio code paths.

## Example

See `example-workflows/puncta_analysis/workflow.py` for a synthetic puncta workflow.
