# CalibratePhasor

`CalibratePhasor` calibrates sample coordinates against a reference acquisition with a known lifetime in nanoseconds.

Sample and reference files must have matching frequency and harmonic metadata but may have different spatial shapes.
PhasorPy reduces the reference acquisition to an intensity-weighted mean or spatial-median phasor center before applying the calibration transform.
Calibration passes the selected harmonic alongside the fundamental frequency; the written metadata retains both values.
