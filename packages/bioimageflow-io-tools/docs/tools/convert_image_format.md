# ConvertImageFormat

`ConvertImageFormat` reads an image and writes it to a requested destination.
The output suffix selects the writer: ordinary paths use imageio,
`.ome.tif`/`.ome.tiff` use tifffile OME-TIFF, and `.ome.zarr` uses the package
minimal OME-Zarr v2 writer.

This is the workflow-level converter for common normalization steps. It can
select one TIFF scene and optionally export one channel, Z plane, or timepoint
before changing the container format.

## Inputs

- `input_image`: image to convert.
- `input_layout`: optional source axis order used when selecting `channel`,
  `z`, or `timepoint`.
- `scene`: optional zero-based TIFF scene index.
- `channel`, `z`, `timepoint`: optional zero-based dimension selections.
- `dimension_order`: optional axis order for OME-TIFF metadata.

## Outputs

- `output_image`: converted image path. The suffix selects the writer:
  ordinary imageio output, OME-TIFF, or minimal OME-Zarr.

## Dependencies and Core Libraries

imageio, tifffile, NumPy, and the package's minimal OME-Zarr writer.

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
        output_image="source.ome.tiff",
        input_layout="CZYX",
        scene=None,
        channel=0,
        z=3,
        timepoint=None,
        dimension_order="YX",
    )
)
```

## Expected Results

The output contains the selected pixel array from the input. OME-TIFF outputs
include the requested dimension order, and OME-Zarr outputs contain minimal
multiscales metadata plus a single array.

## Failure Modes

Unreadable inputs, unsupported imageio outputs, invalid OME axis order, invalid
scene or dimension selections, missing `input_layout` for dimension selection,
and filesystem write failures stop execution. The OME-Zarr writer is
single-scale and uncompressed.
