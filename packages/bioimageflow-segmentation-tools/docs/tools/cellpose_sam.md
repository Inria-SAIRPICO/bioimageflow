# CellposeSAM

`CellposeSAM` segments 2D intensity or channel images with Cellpose-SAM and writes a label mask plus a distinct-label object count.
The `pretrained_model` input is passed to `CellposeModel(pretrained_model=...)` and defaults to `cpsam_v2`, matching Cellpose 4.2.1.1.
For channel images, `channel_axis` explicitly declares whether channels are first or last.

The package import and schema tests stay lightweight because Cellpose is imported only inside `process_row`.

## Model Reuse

Each worker-side `CellposeSAM` instance lazily caches one model by `pretrained_model`.
Repeated rows and retained-engine executions with the same model reuse its weights even when diameter or threshold settings change.
Changing `pretrained_model` replaces the cached model, and `clear_model_cache()` releases the current-process reference explicitly.
Applications can invalidate the remote worker cache by stopping the `segmentation-cellpose-sam` environment.

Runtime tests should use deterministic fake modules first and real model execution only behind `complete` and `model_runtime` markers.
