# BilateralDenoise

`BilateralDenoise` applies edge-preserving bilateral filtering to a finite 2D intensity image.

## Inputs

- `input_image`: 2D intensity image.
- `sigma_color`: positive photometric standard deviation.
- `sigma_spatial`: positive spatial standard deviation in pixels.

## Outputs

- `output_image`: denoised float TIFF with the input shape.

The implementation uses `skimage.restoration.denoise_bilateral` with no channel axis.
