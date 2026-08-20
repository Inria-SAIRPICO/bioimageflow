# TotalVariationDenoise

`TotalVariationDenoise` applies Chambolle total-variation denoising to a finite 2D intensity image.

## Inputs

- `input_image`: 2D intensity image.
- `weight`: positive denoising weight; larger values produce stronger denoising.

## Outputs

- `output_image`: denoised float TIFF with the input shape.

The implementation uses `skimage.restoration.denoise_tv_chambolle` with no channel axis.
