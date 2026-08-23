# Nagini3DSegment

`Nagini3DSegment` runs `nagini3D==0.2.3` on a finite single-channel ZYX TIFF volume.

The required model bundle contains `config.yaml`, `thresholds.yaml`, and the selected weights file.
The tool exposes device, per-axis tile count, anisotropy, threshold overrides, snake refinement, and Otsu-assisted refinement.

Outputs are a uint32 label volume, float32 probability volume, model-provenance JSON, and a compressed surface archive containing `points`, `facets`, `values`, `centers`, `params`, `curvature_positions`, and `curvature_values`.
Surface and curvature object counts must match the number of positive mask labels.

The NAGINI-3D runtime is AGPL-3.0.
Review its license before redistributing a runtime environment; BioImageFlow does not bundle models or datasets.
