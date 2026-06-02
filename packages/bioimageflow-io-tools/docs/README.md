# BioImageFlow IO Tools

Lightweight image IO tools:

- `ReadImage`: reads an image with `imageio` and writes a workflow-local copy.
- `ReadImageMetadata`: reports shape, dtype, dimensionality, and a lightweight
  axes guess.
- `ValidateImageLayout`: checks that a declared axis layout matches an image
  and includes required axes.
- `ConvertImageFormat`: converts images to ordinary imageio outputs,
  OME-TIFF, or minimal OME-Zarr based on the output suffix, with optional scene
  and dimension selection before export.
- `ConvertToOmeTiff`: converts an image file to OME-TIFF with `tifffile`.
- `ConvertToOmeZarr`: converts an image file to a single-scale uncompressed
  OME-Zarr/Zarr v2 directory.
- `SelectScene`: extracts scene 0 from ordinary images or a TIFF series by
  index.
- `SelectTimepoint`: selects one T index from a declared layout.
- `SelectChannel`: selects one C index from a declared layout.
- `SelectZRange`: selects a Python-style start-inclusive, stop-exclusive Z
  slab from a declared layout.
- `SelectDimensions`: selects optional time, channel, and z indices.
Heavy BioIO-backed readers and converters are intentionally isolated for a future
environment-specific implementation.
