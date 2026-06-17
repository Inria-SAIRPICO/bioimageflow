# bioimageflow-segmentation-tools

`bioimageflow-segmentation-tools` contains segmentation wrappers for classical
threshold/watershed workflows and optional deep-learning models. It is the
package to install when a workflow needs label images from intensity,
probability, or binary inputs.

Install-time libraries are imageio, NumPy, and scikit-image.
Classical tool runtime environments include tifffile where file-format handling needs it.
Cellpose, StarDist, TensorFlow, and other model-runtime dependencies live in isolated `EnvironmentSpec` environments and are imported inside `process_row`, so graph construction and schema tests remain lightweight.

## Tools

- <a href="tools/threshold_segment.md">ThresholdSegment</a>: threshold and connected
  component labeling.
- <a href="tools/otsu_threshold_segment.md">OtsuThresholdSegment</a>: global Otsu thresholding
  with connected-component labeling.
- <a href="tools/local_threshold_segment.md">LocalThresholdSegment</a>: Sauvola adaptive
  thresholding with connected-component labeling.
- <a href="tools/watershed_segment.md">WatershedSegment</a>: marker-controlled foreground
  splitting.
- <a href="tools/distance_watershed_segment.md">DistanceWatershedSegment</a>: marker-free
  distance-transform watershed.
- <a href="tools/split_touching_objects.md">SplitTouchingObjects</a>: split clumped label masks
  with distance watershed semantics.
- <a href="tools/filter_labels.md">FilterLabels</a>: filter labels by area, border contact,
  intensity, and shape.
- <a href="tools/postprocess_labels.md">PostprocessLabels</a>: minimum-size filtering and
  sequential relabeling.
- <a href="tools/cellpose3.md">Cellpose3</a>: Cellpose v3 pretrained model wrapper.
- <a href="tools/stardist_segmenter.md">StarDistSegmenter</a>: StarDist 2D pretrained
  model wrapper.

## Demo Workflow

- <a href="workflows/bbbc038_segmentation_benchmark.md">BBBC038 segmentation benchmark</a>:
  synthetic nuclei segmentation with a path to public BBBC038 validation.

## Tests and Demo Data

Run package tests with:

```bash
uv run pytest packages/bioimageflow-segmentation-tools/tests
```

Fast tests execute only the classical tools on generated images and verify that heavy tools build workflow graphs without importing model dependencies.
Optional model-runtime validation should be marked `complete`, `wetlands`, and `model_runtime`; public datasets additionally use `public_data` and define expected object-count or benchmark ranges.
