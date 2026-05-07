# BioImageFlow IO Tools

Lightweight Phase 1 image IO tools:

- `ReadImage`: reads an image with `imageio` and writes a workflow-local copy.
- `SelectDimensions`: selects optional time, channel, and z indices.
- `WriteOmeTiff`: writes OME-TIFF with `tifffile`.
- `WriteOmeZarr`: writes a single-scale uncompressed OME-Zarr/Zarr v2 directory.

Heavy BioIO-backed readers and writers are intentionally isolated for a future
environment-specific implementation.
