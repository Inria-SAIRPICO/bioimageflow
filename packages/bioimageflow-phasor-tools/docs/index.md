# bioimageflow-phasor-tools

`bioimageflow-phasor-tools` provides a small composable FLIM phasor-analysis surface backed by PhasorPy.

## Tools

- [PtuToPhasor](tools/ptu_to_phasor.md): convert a selected PTU histogram to Phasor OME-TIFF.
- [SdtToPhasor](tools/sdt_to_phasor.md): convert a selected SDT dataset to Phasor OME-TIFF.
- [CalibratePhasor](tools/calibrate_phasor.md): calibrate sample coordinates against a known-lifetime reference.
- [FilterPhasor](tools/filter_phasor.md): median-filter and intensity-threshold coordinates.
- [PhasorToApparentLifetime](tools/phasor_to_apparent_lifetime.md): write phase and modulation lifetime images.

## Workflow

- [FLIM phasor analysis](workflows/flim_phasor_analysis.md): PTU conversion, calibration, filtering, and lifetime generation with an SDT conversion variant.

Phasor OME-TIFF is an exchange format, not a replacement for the original acquisition.
