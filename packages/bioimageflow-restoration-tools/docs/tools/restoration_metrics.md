# RestorationMetrics

`RestorationMetrics` compares clean, degraded, and restored images.

It reports MSE, PSNR, and residual-noise estimates for paired restoration validation images.

The images must be 2D, have identical shapes, and contain only finite values.
`data_range` may specify the positive intensity range used for PSNR; when omitted, the tool derives it from a nonconstant clean reference and rejects constant references.
