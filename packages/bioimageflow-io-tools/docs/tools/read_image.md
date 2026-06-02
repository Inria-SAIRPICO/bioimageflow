# ReadImage

`ReadImage` reads an image with imageio and writes a workflow-local copy. It is
used to normalize external paths into BioImageFlow's output templating and
storage conventions before downstream processing.

Input is `input_image`. Output is `output_image`, named from the input stem
unless explicitly supplied by the workflow. Pixel values and shape are expected
to remain unchanged for formats imageio can round-trip.

Use it when a workflow should own a copy of the input artifact. It is not a
metadata reader and does not infer biological axes. Missing files or unsupported
formats fail through imageio.

## Dependencies and Core Libraries

BioImageFlow core APIs, imageio, NumPy through imageio readers, and tifffile
when the selected imageio backend uses it.

## Assumptions

The source image is readable by imageio, and copying the image without explicit
axis metadata is sufficient for the downstream workflow.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ReadImage

ReadImage().process_row(Arguments(input_image="source.tif", output_image="copy.tif"))
```

## Expected Results

`copy.tif` exists and contains the same array values that imageio read from
`source.tif`.

## Failure Modes

Missing files, unsupported formats, unreadable paths, or unwritable output
directories fail through imageio or filesystem errors.
