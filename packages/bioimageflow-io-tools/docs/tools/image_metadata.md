# ReadImageMetadata

`ReadImageMetadata` inspects image headers without loading the full pixel array when the reader supports it.
It reports reader-provided `shape`, `dtype`, axes, channel names, and physical pixel sizes with a separate unit projection.

## Inputs

- `input_image`: image file to inspect.

## Outputs

- `shape`, `dtype`, and `ndim`: array metadata read from the file.
- `axes`: reader-provided axes, sample axes supported by actual TIFF or compatible ImageIO color evidence, and `?` for each ambiguous dimension; matching partial axes keep their known dimensions.
- `channel_names`: metadata names, generated C-axis names, or RGB(A) sample names when available.
- `pixel_sizes`: unchanged X, Y, and Z OME physical size numbers when available; otherwise `None`.
- `pixel_size_units`: separate X, Y, and Z unit strings, preserving explicit OME units; a present size without an explicit unit defaults to µm, while absent calibration has unit `None`.

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
