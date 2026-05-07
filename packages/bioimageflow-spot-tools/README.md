# BioImageFlow Spot Tools

Phase 3 tools for puncta and spot quantification.

## Tools

- `DetectSpots`: detects local maxima after DoG/LoG-compatible filtering and writes a label image plus a CSV table.
- `AssignSpotsToLabels`: samples a label image at each detected spot coordinate.
- `SpotSummary`: aggregates spot counts and intensities per label.

Big-FISH is optional evaluation-only support and is not a normal package dependency. The default tools and tests use lightweight NumPy/imageio code paths.

## Example

See `example-workflows/phase3_puncta/workflow.py` for a synthetic puncta workflow.
