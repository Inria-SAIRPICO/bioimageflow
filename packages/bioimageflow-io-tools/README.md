# bioimageflow-io-tools

Optional BioImageFlow package for image ingestion, metadata inspection, axis validation, slicing, lightweight format conversion, and explicit bioio/plugin-backed conversion.

Core libraries for lightweight tools are imageio, NumPy, and tifffile.
`BioIOConvertImage` and `ConvertToOmeZarr` declare a separate pinned BioIO/plugin-backed runtime for broad microscopy formats and maintained OME-Zarr writing.

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
Use `ConvertImageFormat` only for ordinary imageio conversions.
Use the dedicated OME-TIFF and OME-Zarr tools for OME output; they use reader-provided axes when unambiguous and otherwise require `dimension_order`.
