# HotspotToSpots

`HotspotToSpots` converts a 2D hotspot image into spot dataframe rows.
Eight-connected regions strictly above `threshold` become one spot each, so pixels touching diagonally belong to the same spot.

Keep this tool public because `HotspotDetection` produces an image-like hotspot output, while downstream analyst workflows usually need tabular spot centroids for per-cell counting, intensity summaries, QC plots, or export through explicit table writer tools.

## Inputs

- `hotspot_image`: 2D hotspot score or mask image.
- `threshold`: minimum value included in connected components.

## Outputs

- `spot_id`, `y`, `x`, `intensity`, `score`, `area`, and `label`.
- `spot_count`.

No spot CSV artifact is written because BioImageFlow records these rows in the output dataframe.
If no connected components pass the threshold, direct `process_row()` returns an empty list and workflow execution records `spot_count=0` as manifest-only scalar metadata.

## Dependencies and Core Libraries

imageio, NumPy, and the pinned SciPy `ndimage` labeling and region-measurement functions from the `hotspot` environment.

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
Blank hotspot masks produce no dataframe rows; the selected v1 record manifest and run node result expose `spot_count=0` as `scalar_output` metadata.

## Failure Modes

Unreadable images, non-2D or non-numeric data, non-finite image values, and non-finite thresholds raise errors.
