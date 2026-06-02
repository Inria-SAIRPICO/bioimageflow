# BioImageFlow Segmentation Tools

Segmentation-focused tool package for BioImageFlow.

## Tools

- `Cellpose3`: Cellpose v3 pretrained model wrapper.
- `StarDistSegmenter`: StarDist 2D pretrained model wrapper.
- `ThresholdSegment`: threshold an intensity image and label connected foreground objects.
- `OtsuThresholdSegment`: compute a global Otsu threshold and label foreground objects.
- `LocalThresholdSegment`: compute a Sauvola local threshold and label foreground objects.
- `WatershedSegment`: split foreground regions from marker labels or connected components.
- `DistanceWatershedSegment`: split foreground with marker-free distance-transform watershed.
- `SplitTouchingObjects`: split clumped labels using distance-transform watershed.
- `FilterLabels`: remove labels by area, border contact, intensity, and shape constraints.
- `PostprocessLabels`: remove small labels and relabel label images sequentially.

Heavy model dependencies are declared in isolated `EnvironmentSpec` objects and imported only inside `process_row`. Importing this package does not require Cellpose, TensorFlow, StarDist, or other model packages to be installed in the main process.

## Example

```python
from bioimageflow_segmentation_tools import ThresholdSegment

segment = ThresholdSegment()
node = segment(input_image=raw["path"], threshold=128.0)
```
