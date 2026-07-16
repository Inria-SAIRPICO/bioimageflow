# CellposeSAM

`CellposeSAM` segments 2D intensity images with Cellpose-SAM model runtimes and writes a label mask plus object count.

The package import and schema tests stay lightweight because Cellpose is imported only inside `process_row`.

## Model Reuse

Each worker-side `CellposeSAM` instance lazily caches one model by `model_type`.
Repeated rows and retained-engine executions with the same model reuse its weights even when diameter or threshold settings change.
Changing `model_type` replaces the cached model, and `clear_model_cache()` releases the current-process reference explicitly.
Applications can invalidate the remote worker cache by stopping the `segmentation-cellpose-sam` environment.

Runtime tests should use deterministic fake modules first and real model execution only behind `complete` and `model_runtime` markers.
