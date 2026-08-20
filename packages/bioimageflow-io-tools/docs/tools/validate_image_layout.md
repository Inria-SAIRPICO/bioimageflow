# ValidateImageLayout

`ValidateImageLayout` checks that a declared axis layout matches the image
dimensionality and contains required axes. It can also enforce a minimum size
for every declared axis.

## Inputs

- `input_image`: image file whose dimensions should be checked.
- `layout`: declared axis order, for example `YX`, `ZYX`, `CZYX`, `TCYX`, or
  `TCZYX`.
- `required_axes`: optional string of axes that must be present.
- `min_size`: optional minimum size for every axis.

## Outputs

- `valid`: true when validation passes.
- `axes`: normalized uppercase layout.
- `shape`: image shape as a list of integers.

## Dependencies and Core Libraries

imageio and NumPy-style array shape inspection.

Invalid layouts raise `ValueError` instead of returning `valid=False`, so
downstream workflow rows do not continue with ambiguous dimensions.

## Assumptions

Valid axis names are `T`, `C`, `Z`, `Y`, `X`, and a final RGB(A) sample axis `S`.
The tool validates the declared layout against the array shape; it does not infer whether the declaration is biologically correct.

Use it before tools that depend on a declared layout, especially explicit time,
channel, or Z selection.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ValidateImageLayout

ValidateImageLayout().process_row(
    Arguments(input_image="source.tif", layout="TCYX", required_axes="TC")
)
```

## Expected Results

Valid layouts return the normalized axes and source shape. Unknown axes,
duplicate axes, missing required axes, length mismatches, and undersized axes
raise validation errors before analysis proceeds.

## Failure Modes

Unknown axes, layout length mismatches, duplicate axes, missing required axes,
and dimensions below `min_size` raise `ValueError`.
