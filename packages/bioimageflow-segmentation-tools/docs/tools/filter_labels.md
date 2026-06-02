# FilterLabels

`FilterLabels` removes labels that fail area, border-contact, intensity, or
shape constraints, then relabels retained objects sequentially.

Inputs include `labels`, `min_area`, `max_area`, `remove_border_touching`,
optional `intensity_image`, `min_mean_intensity`, `min_solidity`, and
`max_eccentricity`. Outputs are `output_labels` and `object_count`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import FilterLabels

result = FilterLabels().process_row(
    Arguments(labels="labels.tif", output_labels="filtered.tif", min_area=20)
)
```

## Inputs

- `labels`: 2D or 3D label image.
- `min_area` and `max_area`: pixel/voxel count limits.
- `remove_border_touching`: remove objects touching an image border.
- `intensity_image` and `min_mean_intensity`: optional intensity filter.
- `min_solidity` and `max_eccentricity`: 2D shape filters.

## Outputs

- `output_labels`: filtered labels relabeled sequentially.
- `object_count`: number of retained objects.

## Dependencies and Core Libraries

imageio, NumPy, and scikit-image region-properties measurement.

## Assumptions

Background is `0`. Solidity and eccentricity are applied only to 2D inputs; 3D
inputs still support area, border, and intensity filters.

## Expected Results

Synthetic fixtures remove small, border-touching, or low-intensity objects and
return sequential label IDs.

## Failure Modes

Shape mismatches between labels and intensity images, unreadable files, and
write failures raise errors.
