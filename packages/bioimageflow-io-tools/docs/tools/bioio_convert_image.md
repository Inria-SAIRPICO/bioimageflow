# BioIOConvertImage

`BioIOConvertImage` converts microscopy image file formats using bioio and installed bioio plugins.
It can read broad plugin-backed formats such as CZI, OME-Zarr, OME-TIFF, TIFF, PNG, and similar sources, then write an output format selected by the output suffix.

Use `BioIOConvertImage` when the input requires bioio reader plugins or broad microscopy format coverage.
Use `ConvertImageFormat` for lightweight workflow-local conversion through imageio, tifffile OME-TIFF, or the package's minimal OME-Zarr writer.

Inputs are `input_image`, `dim_order`, optional `scene`, optional `channel`, optional `z`, and optional `timepoint`.
Output is `output_image`, whose extension controls the writer.
The tool prints source dimensions and squeezes singleton leading dimensions after optional selection.

```python
from bioimageflow_io_tools import BioIOConvertImage

converted = BioIOConvertImage()(
    input_image="sample.czi",
    scene=0,
    channel=1,
    output_image="sample_ch1.ome.tiff",
)
```

Expected result: the selected scene/channel/Z/timepoint is written to the requested output format.

## Dependencies and Core Libraries

BioImageFlow core APIs, bioio, bioio OME-TIFF and OME-Zarr writers, bioio CZI/ImageIO/TIFF plugins, NumPy, Pillow, and tifffile.

## Assumptions

The requested bioio reader and writer plugins are installed and the selected dimension order matches the source image.

## Failure Modes

Missing plugins, invalid scene/channel/Z/T indexes, unsupported extensions, or writer errors stop execution.
