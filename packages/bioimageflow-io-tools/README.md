# bioimageflow-io-tools

Optional BioImageFlow package for image ingestion, metadata inspection, axis validation, slicing, lightweight format conversion, and explicit bioio/plugin-backed conversion.

Core libraries for lightweight tools are imageio, NumPy, tifffile, and the package's minimal OME-Zarr writer.
`BioIOConvertImage` declares a separate bioio/plugin-backed runtime for broad formats such as CZI and OME-Zarr.

## Tools

- `ReadImageMetadata`
- `ValidateImageLayout`
- `ConvertImageFormat`
- `BioIOConvertImage`
- `ConvertToOmeTiff`
- `ConvertToOmeZarr`
- `SelectScene`
- `SelectTimepoint`
- `SelectChannel`
- `SelectZRange`
- `SelectDimensions`

Use `BioIOConvertImage` for microscopy formats that need scene, channel, Z, timepoint, or dimension-order handling.
Use `ConvertImageFormat` for simple imageio/tifffile conversions.
