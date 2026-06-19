# bioimageflow-io-tools

`bioimageflow-io-tools` is the optional ingestion and conversion package for
workflow-local image normalization, axis slicing, and simple OME-compatible
output. It is intentionally focused on deterministic file operations rather
than biological analysis.

Core lightweight libraries are imageio, NumPy, tifffile, and a minimal OME-Zarr writer in the package code.
`BioIOConvertImage` uses an explicit bioio/plugin-backed environment for broad microscopy formats such as CZI and OME-Zarr.

## Tools

- [BioIOConvertImage](tools/bioio_convert_image.md): broad bioio/plugin-backed
  format conversion for CZI, OME-Zarr, OME-TIFF, TIFF, PNG, and similar formats.
- [ReadImage](tools/read_image.md): read an image and write a workflow-local
  copy.
- [ReadImageMetadata](tools/read_image_metadata.md): report shape, dtype,
  dimensionality, and a lightweight axes guess.
- [ValidateImageLayout](tools/validate_image_layout.md): check declared layout
  length, required axes, and optional minimum sizes.
- [ConvertImageFormat](tools/convert_image_format.md): select optional scene,
  channel, Z, or T dimensions and convert to imageio outputs, OME-TIFF, or
  minimal OME-Zarr by output suffix.
- [ConvertToOmeTiff](tools/convert_to_ome_tiff.md): convert an image file to
  OME-TIFF with axis metadata.
- [ConvertToOmeZarr](tools/convert_to_ome_zarr.md): convert an image file to a
  single-scale OME-Zarr v2 directory.
- [SelectScene](tools/select_scene.md): extract scene 0 from ordinary images or
  a TIFF series by index.
- [SelectTimepoint](tools/select_timepoint.md): select one T index from a
  declared layout.
- [SelectChannel](tools/select_channel.md): select one C index from a declared
  layout.
- [SelectZRange](tools/select_z_range.md): select a start-inclusive,
  stop-exclusive Z slab from a declared layout.
- [SelectDimensions](tools/select_dimensions.md): select a channel, z plane, or
  timepoint from declared axis layouts.

## Demo Workflow

- [OME normalization workflow](workflows/ome_normalization.md): read, slice,
  and export a tiny OME-compatible image fixture.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
```

The tests generate tiny TIFF arrays and validate shape, axes, Zarr metadata,
and deterministic path outputs. Add committed fixtures only for real-world
layout examples that cannot be represented by generated arrays.
