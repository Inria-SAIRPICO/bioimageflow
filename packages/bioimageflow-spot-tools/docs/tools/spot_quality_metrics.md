# SpotQualityMetrics

`SpotQualityMetrics` computes local quality measurements for detected spots.

## Inputs

- `spots_csv`: table with `y`, `x`, and optional `intensity`.
- `image`: source intensity image.
- `radius`: local window radius.

## Outputs

- `metrics_csv`: original spot columns plus `local_background`, `snr`, and
  `nearest_neighbor_distance`.
- `spot_count`.

## Dependencies and Core Libraries

Python CSV handling, imageio, and NumPy local-window and distance calculations.

## Assumptions

Coordinates are in image pixel units. If `intensity` is missing, the image pixel
at the spot center is used.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import SpotQualityMetrics

SpotQualityMetrics().process_row(
    Arguments(spots_csv="spots.csv", image="image.tif", metrics_csv="quality.csv")
)
```

## Expected Results

Synthetic fixtures report positive SNR for bright spots and exact nearest
neighbor distances.

## Failure Modes

Missing or out-of-bounds coordinates, unreadable images, invalid windows, and
CSV write failures raise errors.
