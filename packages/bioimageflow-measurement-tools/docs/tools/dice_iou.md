# DiceIoU

`DiceIoU` computes foreground pixel overlap metrics for binary masks or label
images. Any non-zero pixel is treated as foreground.

Inputs are `predicted_label_image` and `reference_label_image`. Outputs include
true-positive, false-positive, and false-negative pixel counts plus foreground
IoU and Dice.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import DiceIoU

metrics = DiceIoU().process_row(
    Arguments(predicted_label_image="predicted.tif", reference_label_image="truth.tif")
)
```

## Inputs

- `predicted_label_image`: predicted mask or label image.
- `reference_label_image`: reference mask or label image with the same shape.

## Outputs

- `dice`, `iou`, `true_positive_pixels`, `false_positive_pixels`, and
  `false_negative_pixels`.

## Dependencies and Core Libraries

imageio and NumPy for foreground-mask overlap calculations.

## Assumptions

All non-zero pixels are treated as foreground. Object identity is ignored.

## Expected Results

Synthetic binary masks produce exact pixel counts and deterministic Dice/IoU
values.

## Failure Modes

Shape mismatches or unreadable images raise errors. Empty foreground in both
images reports perfect agreement by convention.
