# SelectTimepoint

`SelectTimepoint` selects one zero-based `T` index from an image using the
declared layout string.

## Inputs

- `input_image`: image with a declared T axis.
- `layout`: axis order for the input array.
- `timepoint`: zero-based T index.

## Outputs

- `output_image`: image with the selected timepoint removed from the array.

## Dependencies and Core Libraries

imageio, NumPy-style slicing, and shared layout validation helpers.

The tool validates that the layout length matches image dimensionality and that
the layout contains a `T` axis.

## Assumptions

The declared layout must match the array dimensions and use supported axis
letters. The tool performs deterministic slicing only.

Use it to make temporal selection explicit before analysis or export.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import SelectTimepoint

SelectTimepoint().process_row(
    Arguments(input_image="tczyx.tif", layout="TCZYX", timepoint=0, output_image="t0.tif")
)
```

## Expected Results

The selected T plane or stack is written without changing other axes. Missing T
axes, invalid layouts, or out-of-range indexes fail before producing output.

## Failure Modes

Layout mismatches and missing `T` axes raise `ValueError`.
Negative and out-of-range indexes raise an explicit `IndexError` before slicing.
