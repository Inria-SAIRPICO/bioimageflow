# CellposeSAM

`CellposeSAM` segments 2D intensity images with Cellpose-SAM model runtimes and writes a label mask plus object count.

The package import and schema tests stay lightweight because Cellpose is imported only inside `process_row`.

Runtime tests should use deterministic fake modules first and real model execution only behind `complete` and `model_runtime` markers.
