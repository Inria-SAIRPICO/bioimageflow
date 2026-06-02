# HotspotToSpots

`HotspotToSpots` converts a 2D hotspot image into a spot coordinate table.
Connected nonzero regions above `threshold` become one spot each.

Keep this tool public because `HotspotDetection` produces an image-like hotspot
output, while downstream analyst workflows usually need tabular spot centroids
for per-cell counting, intensity summaries, QC plots, or export to generic spot
analysis packages.

## Inputs

- `hotspot_image`: 2D hotspot score or mask image.
- `threshold`: minimum value included in connected components.

## Outputs

- `spots_csv`: columns `spot_id`, `y`, `x`, `intensity`, `score`, `area`, and
  `label`.
- `spot_count`.

## Dependencies and Core Libraries

imageio, NumPy, and package-local connected-component traversal.

## Assumptions

Each connected nonzero component corresponds to one spot candidate. Coordinates
are component centroids in pixel units.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import HotspotToSpots

HotspotToSpots().process_row(
    Arguments(hotspot_image="hotspot.tif", threshold=0.5, spots_csv="spots.csv")
)
```

## Expected Results

Synthetic hotspot masks produce one table row per connected component with
stable centroid and area values.

## Failure Modes

Unreadable images, unsupported dimensions, invalid thresholds, and CSV write
failures raise errors.
