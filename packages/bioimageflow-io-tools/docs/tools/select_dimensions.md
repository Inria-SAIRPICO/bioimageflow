# SelectDimensions

`SelectDimensions` slices an image according to a declared axis layout. It
supports `CZYX`, `TCYX`, and `TZYX` layouts and optional `channel`, `z`, and
`timepoint` indexes.

Inputs are `input_image`, `layout`, `channel`, `z`, and `timepoint`. Output is
`output_image`, an intensity image after selection. The tool validates that the
layout string length matches the image dimensionality.

Use it before segmentation, measurement, or export when only one channel,
plane, or timepoint should be analyzed. Wrong layout declarations are the main
failure risk: the tool trusts the declared axis order.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy indexing, and tifffile when reading or
writing TIFF-like data through imageio.

## Assumptions

The declared `layout` matches the array dimensionality and axis order. Indexes
are zero-based and refer to the declared axes.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import SelectDimensions

SelectDimensions().process_row(
    Arguments(
        input_image="czyx.tif",
        layout="CZYX",
        channel=0,
        z=2,
        timepoint=None,
        output_image="plane.tif",
    )
)
```

## Expected Results

The output image contains the selected slice with remaining axes preserved in
their original order.

## Failure Modes

Layout length mismatches raise `ValueError`. Invalid indexes raise indexing
errors. Incorrect but dimensionally valid layouts can silently select the wrong
data.
