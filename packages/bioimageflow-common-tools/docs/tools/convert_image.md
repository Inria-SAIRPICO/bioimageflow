# ConvertImage

`ConvertImage` is a legacy module-level bioio wrapper for broad image-format
conversion. It can read formats supported by the configured bioio plugins and
write OME-TIFF, OME-Zarr, TIFF, PNG, or similar outputs based on the requested
output extension.

The tool is currently importable from `bioimageflow_common_tools.convert_image`,
but it is not re-exported from `bioimageflow_common_tools.__init__`. For new
simple TIFF/OME-TIFF/OME-Zarr workflows, prefer the smaller
`bioimageflow-io-tools` package. Keep this wrapper for broad bioio format
coverage and legacy workflows until it is migrated or retired.

Inputs are `input_image`, `dim_order`, optional `scene`, optional `channel`,
optional `z`, and optional `timepoint`. Output is `output_image`, whose
extension controls the writer. The tool prints source dimensions and squeezes
singleton leading dimensions after optional selection.

Core dependencies are bioio, Pillow, bioio OME-TIFF and OME-Zarr plugins,
bioio CZI/ImageIO/TIFF plugins, NumPy, and tifffile in the `bioio-all`
environment.

```python
from bioimageflow_common_tools.convert_image import ConvertImage

converted = ConvertImage()(
    input_image="sample.czi",
    scene=0,
    channel=1,
    output_image="sample_ch1.ome.tiff",
)
```

Expected result: the selected scene/channel/Z/timepoint is written to the
requested output format. Failure modes include unavailable bioio plugins,
unsupported source formats, invalid scene or axis indexes, incompatible output
extension, and writer failures for large or unsupported arrays.

## Dependencies and Core Libraries

BioImageFlow core APIs, bioio, bioio OME-TIFF and OME-Zarr writers, imageio,
NumPy, Pillow, and tifffile.

## Assumptions

The requested bioio reader and writer plugins are installed and the selected
dimension order matches the source image.

## Minimal Example

The example above converts one selected channel from a CZI file to OME-TIFF.

## Expected Results

The requested output path exists and contains the selected image data in the
format implied by its extension.

## Failure Modes

Missing plugins, invalid scene/channel/Z/T indexes, unsupported extensions, or
writer errors stop execution.
