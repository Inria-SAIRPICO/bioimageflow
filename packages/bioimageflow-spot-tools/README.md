# bioimageflow-spot-tools

Tools for puncta and spot quantification.

## Tools

- `AtlasSpotDetection`: wraps the external Atlas CLI for adaptive 2D TIFF spot detection.
- `DetectSpots`: detects local maxima after DoG/LoG-compatible filtering and returns one dataframe row per spot plus a label image.
- `AssignSpotsToLabels`: samples a label image at each detected spot coordinate.
- `SpotSummary`: aggregates spot counts and intensities per source and label.
- `FilterSpots`: filters spot dataframe rows by intensity, score, radius, and mask.
- `RenderSpots`: renders coordinate rows to binary mask or label images.
- `SpotsToLabels`: combines spot-coordinate rows into one label image.
- `MaskToLabels`: converts each mask row into a connected-component label image.
- `SpotColocalization`: computes a maximum-cardinality, minimum-total-distance one-to-one match between two spot tables.
- `SpotQualityMetrics`: computes annular-background SNR and indexed nearest-neighbor distances.

ATLAS is the primary external spot detection method used by the FISH and parameter-space exploration workflows.
The local spot table utilities support assignment, rendering, filtering, and summary after detection.
All coordinate consumers use finite `(y, x)` values and the same nearest-pixel rule: exact half values round upward.

## Example

See the FISH spot-counting and parameter-space exploration workflows in the main workflow catalog.
