# PhasorToApparentLifetime

`PhasorToApparentLifetime` reads frequency metadata from Phasor OME-TIFF and writes float32 apparent phase and modulation lifetimes in nanoseconds.
The calculation uses the selected harmonic multiplied by the fundamental frequency, while the returned frequency metadata retains that fundamental value.

Undefined or non-physical infinite results are written as NaN.
