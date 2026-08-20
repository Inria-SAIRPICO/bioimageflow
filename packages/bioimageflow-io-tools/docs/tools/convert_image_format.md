# ConvertImageFormat

`ConvertImageFormat` reads an image and writes an ordinary raster format through imageio.
OME-TIFF and OME-Zarr outputs are intentionally handled by their dedicated tools.

This is the workflow-level converter for common normalization steps. It can
select one TIFF scene and optionally export one channel, Z plane, or timepoint
before changing the container format.

Use `ConvertImageFormat` for deterministic lightweight conversion when imageio or tifffile can read the source.
Use `BioIOConvertImage` for broad bioio/plugin-backed formats such as CZI, OME-Zarr, or sources that require dedicated microscopy reader plugins.

## Inputs

- `input_image`: image to convert.
- `input_layout`: optional source axis order used when selecting `channel`,
  `z`, or `timepoint`.
- `scene`: optional zero-based TIFF scene index.
- `channel`, `z`, `timepoint`: optional zero-based dimension selections.

## Outputs

- `output_image`: converted ordinary image path.

## Dependencies and Core Libraries

imageio, tifffile, and NumPy.

## Assumptions

The input array is already in the desired pixel type. The tool changes
container format and metadata, and can remove declared scene/channel/Z/T
dimensions. It does not reorder axes or rescale intensities.

Use it for deterministic workflow-local format conversion without adding
Bio-Formats or a full NGFF stack to the lightweight package environment.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_io_tools import ConvertImageFormat

ConvertImageFormat().process_row(
    Arguments(
        input_image="source.tif",
        output_image="source_selected.tif",
        input_layout="CZYX",
        scene=None,
        channel=0,
        z=3,
        timepoint=None,
    )
)
```

## Expected Results

The output contains the selected pixel array from the input.

## Failure Modes

Unreadable inputs, OME output suffixes, unsupported imageio outputs, invalid scene or dimension selections, missing `input_layout` for selection, and filesystem failures stop execution.
