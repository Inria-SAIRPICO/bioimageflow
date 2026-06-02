# bioimageflow-io-tools

`bioimageflow-io-tools` is the optional ingestion and conversion package for
workflow-local image normalization, axis slicing, and simple OME-compatible
output. It is intentionally focused on deterministic file operations rather
than biological analysis.

Core libraries are imageio, NumPy, tifffile, and a minimal OME-Zarr writer in
the package code. A future Bio-Formats or bioio-backed tool can live here, but
the current public tools are lightweight and suitable for fast CI tests.

## Tools

- [ReadImage](#readimage): read an image and write a workflow-local
  copy.
- [ReadImageMetadata](#readimagemetadata): report shape, dtype,
  dimensionality, and a lightweight axes guess.
- [ValidateImageLayout](#validateimagelayout): check declared layout
  length, required axes, and optional minimum sizes.
- [ConvertImageFormat](#convertimageformat): select optional scene,
  channel, Z, or T dimensions and convert to imageio outputs, OME-TIFF, or
  minimal OME-Zarr by output suffix.
- [ConvertToOmeTiff](#converttoometiff): convert an image file to
  OME-TIFF with axis metadata.
- [ConvertToOmeZarr](#converttoomezarr): convert an image file to a
  single-scale OME-Zarr v2 directory.
- [SelectScene](#selectscene): extract scene 0 from ordinary images or
  a TIFF series by index.
- [SelectTimepoint](#selecttimepoint): select one T index from a
  declared layout.
- [SelectChannel](#selectchannel): select one C index from a declared
  layout.
- [SelectZRange](#selectzrange): select a start-inclusive,
  stop-exclusive Z slab from a declared layout.
- [SelectDimensions](#selectdimensions): select a channel, z plane, or
  timepoint from declared axis layouts.

## Demo Workflow

- [OME normalization workflow](#ome-normalization-workflow): read, slice,
  and export a tiny OME-compatible image fixture.

## Tests and Demo Data

Package tests live in `tests/` and can be run with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
```

The tests generate tiny TIFF arrays and validate shape, axes, Zarr metadata,
and deterministic path outputs. Add committed fixtures only for real-world
layout examples that cannot be represented by generated arrays.
