# BioImageFlow Spot Tools

Tools for puncta and spot quantification.

## Tools

- `DetectSpots`: detects local maxima after DoG/LoG-compatible filtering and writes a label image plus a CSV table.
- `AssignSpotsToLabels`: samples a label image at each detected spot coordinate.
- `SpotSummary`: aggregates spot counts and intensities per label.
- `FilterSpots`: filters spot tables by intensity, score, radius, and mask.
- `RenderSpots`: renders coordinate tables to binary mask or label images.
- `SpotsToLabels`: converts spot coordinates or masks into label images.
- `SpotColocalization`: matches spots between channels by distance.
- `SpotQualityMetrics`: computes SNR, local background, and nearest-neighbor distances.

Big-FISH is optional evaluation-only support and is not a normal package dependency. The default tools and tests use lightweight NumPy/imageio code paths.

## Example

See `example-workflows/puncta_analysis/workflow.py` for a synthetic puncta workflow.
