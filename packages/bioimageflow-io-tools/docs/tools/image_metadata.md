# ReadImageMetadata

`ReadImageMetadata` inspects image headers without loading the full pixel array when the reader supports it.
It reports reader-provided `shape`, `dtype`, axes, channel names, and physical pixel sizes.

## Inputs

- `input_image`: image file to inspect.

## Outputs

- `shape`, `dtype`, and `ndim`: array metadata read from the file.
- `axes`: reader-provided axes, `YXS` for channel-last RGB(A), and `?` for each ambiguous dimension.
- `channel_names`: metadata names, generated C-axis names, or RGB(A) sample names when available.
- `pixel_sizes`: X, Y, and Z physical pixel sizes when TIFF metadata exposes
  them; otherwise `None`.

## Dependencies and Core Libraries

imageio, tifffile, NumPy, and Python XML parsing for OME-TIFF metadata.

## Assumptions

This is a lightweight reader and does not invent biological meanings for unnamed dimensions.

Use it before layout validation or conversion when a workflow needs to branch
or report basic image properties.

## Minimal Example

```python
from bioimageflow_core import Arguments
import bioimageflow_io_tools

metadata = bioimageflow_io_tools.ReadImageMetadata().process_row(Arguments(input_image="source.tif"))
assert metadata.axes == "CZYX"
```

## Expected Results

Unannotated multidimensional TIFFs report ambiguous leading axes, while OME-TIFF axes and physical sizes are preserved.

## Failure Modes

Missing files, unsupported formats, unreadable paths, or unsupported
dimensionality for the axes guess raise the underlying reader or validation
error.
