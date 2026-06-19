# nnInteractive

`nnInteractive` runs prompt-driven segmentation from semicolon-separated `y,x` prompt coordinates and writes a label mask plus object count.

The wrapper accepts an optional checkpoint path and imports `nninteractive` only during execution.

Real runtime validation belongs behind `complete` and `model_runtime` markers.
