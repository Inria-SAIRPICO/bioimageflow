# bioimageflow-segmentation-tools

Segmentation-focused tool package for BioImageFlow.

## Tools

- `Cellpose3`: Cellpose v3 pretrained model wrapper.
- `CellposeSAM`: Cellpose-SAM pretrained model wrapper.
- `StarDistSegmenter`: StarDist 2D pretrained model wrapper.
- `ThresholdSegment`: threshold an intensity image and label connected foreground objects.
- `OtsuThresholdSegment`: compute a global Otsu threshold and label foreground objects.
- `LocalThresholdSegment`: compute a Sauvola local threshold and label foreground objects.
- `WatershedSegment`: split foreground regions from marker labels or connected components.
- `DistanceWatershedSegment`: split foreground with marker-free distance-transform watershed.
- `SplitTouchingObjects`: split clumped labels using distance-transform watershed.
- `FilterLabels`: remove labels by area, border contact, intensity, and shape constraints.
- `PostprocessLabels`: remove small labels and relabel label images sequentially.

Classical connected-component tools use face connectivity by default and treat `image > threshold` as foreground when `above` is enabled. Label inputs are validated as finite, integral, non-negative arrays, and object counts are based on distinct positive IDs rather than the largest ID.

Heavy model dependencies are declared in isolated `EnvironmentSpec` objects and imported only inside `process_row`. Importing this package does not require Cellpose, TensorFlow, StarDist, or other model packages to be installed in the main process.
`Cellpose3`, `CellposeSAM`, and `StarDistSegmenter` lazily keep one model per worker-side tool instance.
Repeated calls with the same model selection reuse the weights; changing `model_type` or `model_name` replaces the cached model, and `clear_model_cache()` releases it explicitly.
Applications can invalidate remote worker caches by stopping the corresponding Wetlands environment.

The former `nnInteractive` wrapper was removed because nnInteractive requires a stateful volumetric inference session; its public point-list-to-2D-mask contract did not represent the upstream API.

## Example

```python
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import ThresholdSegment

segment = ThresholdSegment()
result = segment.process_row(
    Arguments(
        input_image="input.tif",
        threshold=128.0,
        labels="labels.tif",
        above=True,
    )
)
```

Workflow graph construction with `segment(...)` requires installing the main-process `bioimageflow` orchestrator alongside this package.
