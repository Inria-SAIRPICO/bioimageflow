# bioimageflow-phasor-tools

Composable PhasorPy-based tools for fluorescence lifetime imaging microscopy.

The package converts PicoQuant PTU and Becker & Hickl SDT histograms to Phasor OME-TIFF, calibrates coordinates with a known reference, filters noisy phasors, and writes apparent phase and modulation lifetime images.

Phasor OME-TIFF is treated as an exchange and workflow-intermediate artifact.
Retain the original PTU or SDT acquisition as the archival source.

Heavy PhasorPy and format-reader dependencies are installed only in the isolated `spectral-phasorpy` environment.
