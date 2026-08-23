# InstanSegSegment

`InstanSegSegment` performs selected-target 2D nuclei or cell segmentation with `instanseg-torch==0.1.1`.

Use `model_name` for an official model or `model_path` for a local model bundle; a local path takes precedence.
Named models use InstanSeg's download cache on first execution, while the provenance JSON records the resolved selection and input digest.

The tool accepts YX, CYX, or explicitly declared YXC arrays, optional channel IDs and pixel size, `small`, `medium`, or automatic processing, and CPU/CUDA/MPS device selection.
It returns one uint32 YX mask for `target="nuclei"` or `target="cells"` and rejects cell requests for nucleus-only models.

Whole-slide inference is intentionally outside the first contract.
