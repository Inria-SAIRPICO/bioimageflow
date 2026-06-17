# bioimageflow-io-tools

`bioimageflow-io-tools` is the optional ingestion and conversion package for
workflow-local image normalization, axis slicing, and simple OME-compatible
output. It is intentionally focused on deterministic file operations rather
than biological analysis.

Core libraries are imageio, NumPy, tifffile, and a minimal OME-Zarr writer in
the package code. A future Bio-Formats or bioio-backed tool can live here, but
the current public tools are lightweight and suitable for fast CI tests.

## Tools

- <a href="tools/read_image.md">ReadImage</a>: read an image and write a workflow-local
  copy.
- <a href="tools/read_image_metadata.md">ReadImageMetadata</a>: report shape, dtype,
  dimensionality, and a lightweight axes guess.
- <a href="tools/validate_image_layout.md">ValidateImageLayout</a>: check declared layout
  length, required axes, and optional minimum sizes.
- <a href="tools/convert_image_format.md">ConvertImageFormat</a>: select optional scene,
  channel, Z, or T dimensions and convert to imageio outputs, OME-TIFF, or
  minimal OME-Zarr by output suffix.
- <a href="tools/convert_to_ome_tiff.md">ConvertToOmeTiff</a>: convert an image file to
  OME-TIFF with axis metadata.
- <a href="tools/convert_to_ome_zarr.md">ConvertToOmeZarr</a>: convert an image file to a
  single-scale OME-Zarr v2 directory.
- <a href="tools/select_scene.md">SelectScene</a>: extract scene 0 from ordinary images or
  a TIFF series by index.
- <a href="tools/select_timepoint.md">SelectTimepoint</a>: select one T index from a
  declared layout.
- <a href="tools/select_channel.md">SelectChannel</a>: select one C index from a declared
  layout.
- <a href="tools/select_z_range.md">SelectZRange</a>: select a start-inclusive,
  stop-exclusive Z slab from a declared layout.
- <a href="tools/select_dimensions.md">SelectDimensions</a>: select a channel, z plane, or
  timepoint from declared axis layouts.

## Demo Workflow

- <a href="workflows/ome_normalization.md">OME normalization workflow</a>: read, slice,
  and export a tiny OME-compatible image fixture.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
```

The tests generate tiny TIFF arrays and validate shape, axes, Zarr metadata,
and deterministic path outputs. Add committed fixtures only for real-world
layout examples that cannot be represented by generated arrays.
