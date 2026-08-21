# HotspotDetection

## Purpose

`HotspotDetection` detects sparse hotspots in microscopy intensity images using
the SAIRPICO `hotSpotDetection` command. It is intended for spot-like signal
detection where a patch radius, neighborhood radius, and false-alarm p-value are
the primary controls.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `patch_size`: patch radius passed as `-m`. Default `3`; minimum `1`.
- `neighborhood_size`: neighborhood radius passed as `-n`. Default `5`;
  minimum `1`.
- `p_value`: false-alarm p-value passed as `-pv`. Default `0.2`; range `0.0`
  to `1.0`.

## Outputs

- `output_image`: hotspot image. The default template is
  `{input_image.stem}_hotspot.tif`.

## Assumptions

- The input is a microscopy intensity image with sparse local hotspot signal.
- Output semantics are intensity-valued according to the wrapper schema, not a
  label table.
- The p-value and neighborhood settings are tuned to the signal and background
  statistics of the image.

## Dependencies and Core Libraries

- BioImageFlow core processing and image schema classes.
- SAIRPICO `hotspot` environment with `bioimageit::hotspot==1.0.0` and pinned scientific image I/O dependencies; Linux additionally pins the libtiff 4.4 ABI required by the published Linux executable.
- External command: `hotSpotDetection`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import HotspotDetection

result = HotspotDetection().process_row(
    Arguments(
        input_image="spots.tif",
        patch_size=3,
        neighborhood_size=5,
        p_value=0.2,
        output_image="spots_hotspot.tif",
    )
)
```

## Expected Results

The tool writes a hotspot image and returns its path as `output_image`. The
example invokes `hotSpotDetection` with `-m 3`, `-n 5`, and `-pv 0.2`.

## Failure Modes

- `hotSpotDetection` is not available in the active environment.
- Patch and neighborhood sizes are not positive integers, or the p-value is not finite and between `0.0` and `1.0`.
- Input image format or dimensionality is unsupported by the binary.
- Threshold parameters produce no useful detections or excessive detections.
- The subprocess exits non-zero or output writing fails.
