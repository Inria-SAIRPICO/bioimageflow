# ReadImageMetadata

`ReadImageMetadata` reads an image and returns lightweight metadata for
ingestion checks. It reports `shape`, `dtype`, `ndim`, and a pragmatic `axes`
guess derived from dimensionality, generated channel names, and available
pixel-size metadata.

## Inputs

- `input_image`: image file to inspect.

## Outputs

- `shape`, `dtype`, and `ndim`: array metadata read from the file.
- `axes`: pragmatic axis guess using common BioImageFlow defaults: `YX`, `ZYX`,
  `CZYX`, and `TCZYX`.
- `channel_names`: generated names such as `channel_0` when a C axis is
  inferred.
- `pixel_sizes`: X, Y, and Z physical pixel sizes when TIFF metadata exposes
  them; otherwise `None`.

## Dependencies and Core Libraries

imageio, tifffile, NumPy, and Python XML parsing for OME-TIFF metadata.

## Assumptions

This is a lightweight reader. It does not normalize metadata across every
microscopy container, and axis names are guesses unless the file format exposes
stronger metadata.

Use it before layout validation or conversion when a workflow needs to branch
or report basic image properties.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ReadImageMetadata

metadata = ReadImageMetadata().process_row(Arguments(input_image="source.tif"))
assert metadata.axes == "CZYX"
```

## Expected Results

Synthetic 4D fixtures report the expected shape, dtype, `CZYX` axes, generated
channel names, and empty pixel sizes. Real OME-TIFF images should additionally
populate pixel sizes when physical-size tags are present.

## Failure Modes

Missing files, unsupported formats, unreadable paths, or unsupported
dimensionality for the axes guess raise the underlying reader or validation
error.
