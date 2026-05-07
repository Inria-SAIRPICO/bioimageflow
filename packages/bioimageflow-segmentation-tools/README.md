# BioImageFlow Segmentation Tools

Segmentation-focused tool package for BioImageFlow.

## Tools

- `Cellpose3`: Cellpose v3 pretrained model wrapper.
- `StarDistSegmenter`: StarDist 2D pretrained model wrapper.
- `ThresholdSegment`: threshold an intensity image and label connected foreground objects.
- `WatershedSegment`: split foreground regions from marker labels or connected components.
- `PostprocessLabels`: remove small labels and relabel label images sequentially.

Heavy model dependencies are declared in isolated `EnvironmentSpec` objects and imported only inside `process_row`. Importing this package does not require Cellpose, TensorFlow, StarDist, or other model packages to be installed in the main process.

## Example

```python
from bioimageflow_segmentation_tools import ThresholdSegment

segment = ThresholdSegment()
node = segment(input_image=raw["path"], threshold=128.0)
```
