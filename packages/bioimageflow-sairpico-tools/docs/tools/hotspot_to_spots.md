# HotspotToSpots

`HotspotToSpots` converts a 2D hotspot image into spot dataframe rows.
Connected nonzero regions above `threshold` become one spot each.

Keep this tool public because `HotspotDetection` produces an image-like hotspot output, while downstream analyst workflows usually need tabular spot centroids for per-cell counting, intensity summaries, QC plots, or export through explicit table writer tools.

## Inputs

- `hotspot_image`: 2D hotspot score or mask image.
- `threshold`: minimum value included in connected components.

## Outputs

- `spot_id`, `y`, `x`, `intensity`, `score`, `area`, and `label`.
- `spot_count`.

No spot CSV artifact is written because BioImageFlow records these rows in the output dataframe.

## Dependencies and Core Libraries

imageio, NumPy, and package-local connected-component traversal.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import HotspotToSpots

HotspotToSpots().process_row(
    Arguments(hotspot_image="hotspot.tif", threshold=0.5)
)
```

## Expected Results

Synthetic hotspot masks produce one dataframe row per connected component with stable centroid and area values.

## Failure Modes

Unreadable images, unsupported dimensions, and invalid thresholds raise errors.
