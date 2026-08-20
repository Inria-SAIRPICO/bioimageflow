# ObjectMatchingMetrics

`ObjectMatchingMetrics` builds one predicted/reference overlap contingency and greedily takes the best non-overlapping object pairs above `iou_threshold`.

Inputs are `predicted_label_image`, `reference_label_image`, and
`iou_threshold`. Outputs report predicted/reference counts, matched and
unmatched counts, and mean matched IoU/Dice.

Use it for deterministic instance-segmentation benchmark checks on small or
medium label images.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_measurement_tools import ObjectMatchingMetrics

metrics = ObjectMatchingMetrics().process_row(
    Arguments(
        predicted_label_image="predicted.tif",
        reference_label_image="reference.tif",
        iou_threshold=0.5,
    )
)
```

## Inputs

- `predicted_label_image`: predicted label image.
- `reference_label_image`: reference label image with the same shape.
- `iou_threshold`: finite minimum object IoU for a match, between `0` and `1` inclusive.

## Outputs

- scalar counts for predicted, reference, matched, and unmatched objects.
- mean matched IoU and Dice over accepted matches.

## Dependencies and Core Libraries

imageio and NumPy for validated labels and vectorized overlap contingency calculations.

## Assumptions

Labels use `0` as background. Greedy matching is deterministic and adequate for
smoke tests and simple benchmarks.

## Expected Results

Synthetic fixtures with overlapping labels produce exact match,
false-positive, and false-negative counts.

## Failure Modes

Shape mismatches, unreadable images, and invalid labels raise errors before
metrics are returned.
