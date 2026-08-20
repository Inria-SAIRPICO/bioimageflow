# SelectChannel

`SelectChannel` selects one zero-based `C` index from an image using the
declared layout string.

## Inputs

- `input_image`: image with a declared C axis.
- `layout`: axis order for the input array.
- `channel`: zero-based C index.

## Outputs

- `output_image`: image with the selected channel removed from the array.

## Dependencies and Core Libraries

imageio, NumPy-style slicing, and shared layout validation helpers.

The tool validates that the layout length matches image dimensionality and that
the layout contains a `C` axis.

## Assumptions

The declared layout must match the array dimensions and use supported axis
letters. The tool performs deterministic slicing only.

Use it to make channel choice explicit before segmentation, measurement, or
format conversion.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import SelectChannel

SelectChannel().process_row(
    Arguments(input_image="czyx.tif", layout="CZYX", channel=1, output_image="c1.tif")
)
```

## Expected Results

The selected channel is written with all non-C axes preserved. Missing C axes,
invalid layouts, or out-of-range indexes raise errors.

## Failure Modes

Layout mismatches and missing `C` axes raise `ValueError`.
Negative and out-of-range indexes raise an explicit `IndexError` before slicing.
