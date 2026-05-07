Segmentation Tools
==================

``bioimageflow-segmentation-tools`` contains segmentation-specific
``ProcessingTool`` implementations:

* ``Cellpose3`` for Cellpose v3 pretrained segmentation.
* ``StarDistSegmenter`` for StarDist 2D pretrained segmentation.
* ``ThresholdSegment`` for threshold-based connected-component labels.
* ``WatershedSegment`` for marker-controlled foreground splitting.
* ``PostprocessLabels`` for small-label filtering and sequential relabeling.

The Cellpose and StarDist wrappers declare isolated environments and import
their model libraries only inside ``process_row``. The package can therefore be
imported, serialized, and used for graph construction without installing heavy
model dependencies in the main process.
