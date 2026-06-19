# bioimageflow-io-tools

Optional BioImageFlow package for image ingestion, metadata inspection, axis validation, slicing, lightweight format conversion, and explicit bioio/plugin-backed conversion.

Core libraries for lightweight tools are imageio, NumPy, tifffile, and the package's minimal OME-Zarr writer.
`BioIOConvertImage` declares a separate bioio/plugin-backed runtime for broad formats such as CZI and OME-Zarr.

## Tools

- `ReadImage`
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

## Tests

Run package tests with:

```bash
uv run pytest packages/bioimageflow-io-tools/tests
```

The tests create small synthetic images and validate output paths, axis
selection, OME-TIFF metadata, OME-Zarr structure, and failure modes.
