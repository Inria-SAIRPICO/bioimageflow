# bioimageflow-spot-tools

Tools for puncta and spot quantification.

## Tools

- `AtlasSpotDetection`: wraps the external Atlas CLI for adaptive 2D TIFF spot detection.
- `DetectSpots`: detects local maxima after DoG/LoG-compatible filtering and returns one dataframe row per spot plus a label image.
- `AssignSpotsToLabels`: samples a label image at each detected spot coordinate.
- `SpotSummary`: aggregates spot counts and intensities per label.
- `FilterSpots`: filters spot dataframe rows by intensity, score, radius, and mask.
- `RenderSpots`: renders coordinate rows to binary mask or label images.
- `SpotsToLabels`: combines spot-coordinate rows into one label image.
- `MaskToLabels`: converts each mask row into a connected-component label image.
- `SpotColocalization`: matches two upstream spot dataframes between channels by distance.
- `SpotQualityMetrics`: computes SNR, local background, and nearest-neighbor distances.

ATLAS is the primary external spot detection method used by the FISH and parameter-space exploration workflows.
The local spot table utilities support assignment, rendering, filtering, and summary after detection.

## Example

See the FISH spot-counting and parameter-space exploration workflows in the main workflow catalog.
