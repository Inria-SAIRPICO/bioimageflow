# PtuToPhasor

`PtuToPhasor` reads a T3 imaging-mode PicoQuant PTU histogram using `signal_from_ptu`, selects its channel, frame, detector-time index, and harmonic, and writes mean/real/imag coordinates with frequency metadata to Phasor OME-TIFF.

The PTU file must remain available as the archival input.
