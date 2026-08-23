# NAGINI-3D CAPS Segmentation

The `nagini3d_caps_segmentation` workflow applies a local NAGINI-3D model bundle to a CAPS-style ZYX volume.
It produces a uint32 instance mask, a float32 probability image, stable surface arrays, and an object-level surface summary.

Run it from the repository root:

```bash
python example_workflows/nagini3d_caps_segmentation/workflow.py --input-volume data/caps_volume.tif --model-bundle models/nagini_caps
```

NAGINI-3D is AGPL-3.0 software.
Installing or distributing the isolated NAGINI runtime may impose AGPL obligations, so this adapter is opt-in and the upstream license must be reviewed for the intended deployment.
The data manifest identifies the official CAPS and model sources and requires SHA-256 verification of the selected volume and complete model bundle.
