# FLIM Phasor Analysis

The example workflow converts sample and reference PTU acquisitions to Phasor OME-TIFF, calibrates the sample with the known reference lifetime, applies median filtering and intensity thresholding, and produces phase and modulation lifetime TIFFs.

An SDT input can use `SdtToPhasor` and then enter the same calibration and filtering chain.
