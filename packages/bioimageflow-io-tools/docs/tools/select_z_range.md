# SelectZRange

`SelectZRange` selects a Z slab from an image using Python-style slice bounds:
`start_z` is included and `stop_z` is excluded. Leaving `stop_z` empty keeps
planes through the end of the Z axis.

## Inputs

- `input_image`: image with a declared Z axis.
- `layout`: axis order for the input array.
- `start_z`: first Z index to keep.
- `stop_z`: optional first Z index to exclude.

## Outputs

- `output_image`: image containing the selected Z slab.

## Dependencies and Core Libraries

imageio, NumPy-style slicing, and shared layout validation helpers.

The tool validates that the layout length matches image dimensionality and
contains a `Z` axis.

## Assumptions

The declared layout must match the array dimensions and use supported axis
letters. Z ranges use Python slice semantics.

Use it to extract a projection-ready Z subset or a smaller volume before
downstream analysis.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import SelectZRange

SelectZRange().process_row(
    Arguments(input_image="zyx.tif", layout="ZYX", start_z=2, stop_z=6, output_image="z2_6.tif")
)
```

## Expected Results

The output preserves all non-Z axes and includes only the requested Z planes.
Missing Z axes or reversed ranges raise errors.

## Failure Modes

Layout mismatches, missing `Z` axes, and `stop_z < start_z` raise `ValueError`.
Out-of-range slice bounds follow normal NumPy slicing behavior.
