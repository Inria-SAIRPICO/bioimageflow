# bioimageflow-io-tools

Optional BioImageFlow package for deterministic image ingestion, metadata
inspection, axis validation, slicing, and lightweight format conversion.

Core libraries: imageio, NumPy, tifffile, and the package's minimal OME-Zarr
writer. The package intentionally avoids heavy Bio-Formats dependencies for the
current P0 tools so tests can run quickly with generated TIFF fixtures.

## Tools

- `ReadImage`
- `ReadImageMetadata`
- `ValidateImageLayout`
- `ConvertImageFormat`
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
