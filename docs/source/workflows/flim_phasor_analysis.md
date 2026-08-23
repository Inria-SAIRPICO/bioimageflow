# FLIM Phasor Analysis

The `flim_phasor_analysis` workflow reproduces the basic PhasorPy FLIM path for PicoQuant PTU acquisitions: convert sample and reference histograms to Phasor OME-TIFF, calibrate against a known reference lifetime, filter and threshold the calibrated coordinates, and calculate phase and modulation lifetime images.

Run it from the repository root:

```bash
python example_workflows/flim_phasor_analysis/workflow.py --sample-ptu data/sample.ptu --reference-ptu data/reference.ptu --reference-lifetime-ns 4.2
```

The Phasor OME-TIFF intermediate keeps frequency, harmonic, and processing descriptions available between independently executable tools.
Becker & Hickl SDT data can enter the same downstream chain through `SdtToPhasor`.
