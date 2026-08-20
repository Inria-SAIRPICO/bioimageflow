# bioimageflow-segmentation-tools

`bioimageflow-segmentation-tools` contains segmentation wrappers for classical
threshold/watershed workflows and optional deep-learning models. It is the
package to install when a workflow needs label images from intensity,
probability, or binary inputs.

Install-time libraries are imageio, NumPy, and scikit-image.
Classical tool runtime environments include tifffile where file-format handling needs it.
Cellpose, StarDist, TensorFlow, and other model-runtime dependencies live in isolated `EnvironmentSpec` environments and are imported inside `process_row`, so graph construction and schema tests remain lightweight.

## Tools

- [ThresholdSegment](tools/threshold_segment.md): threshold and connected
  component labeling.
- [OtsuThresholdSegment](tools/otsu_threshold_segment.md): global Otsu thresholding
  with connected-component labeling.
- [LocalThresholdSegment](tools/local_threshold_segment.md): Sauvola adaptive
  thresholding with connected-component labeling.
- [WatershedSegment](tools/watershed_segment.md): marker-controlled foreground
  splitting.
- [DistanceWatershedSegment](tools/distance_watershed_segment.md): marker-free
  distance-transform watershed.
- [SplitTouchingObjects](tools/split_touching_objects.md): split clumped label masks
  with distance watershed semantics.
- [FilterLabels](tools/filter_labels.md): filter labels by area, border contact,
  intensity, and shape.
- [PostprocessLabels](tools/postprocess_labels.md): minimum-size filtering and
  sequential relabeling.
- [Cellpose3](tools/cellpose3.md): Cellpose v3 pretrained model wrapper.
- [CellposeSAM](tools/cellpose_sam.md): Cellpose-SAM model wrapper.
- [StarDistSegmenter](tools/stardist_segmenter.md): StarDist 2D pretrained
  model wrapper.

## Demo Workflow

- [BBBC038 segmentation benchmark](workflows/bbbc038_segmentation_benchmark.md):
  comparison of Cellpose v3, Cellpose-SAM, StarDist, and a classical branch on BBBC038-style nuclei masks.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-segmentation-tools/tests
```

Fast tests execute the classical tools on small local fixtures and verify that heavy tools build workflow graphs without importing model dependencies.
Optional model-runtime validation should be marked `complete`, `wetlands`, and `model_runtime`; public datasets additionally use `public_data` and define expected object-count or benchmark ranges.
