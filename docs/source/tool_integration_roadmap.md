# Tool Integration Roadmap

BioImageFlow intentionally starts with a small set of production-oriented tools rather than a broad catalogue of demonstration wrappers.

## Current segmentation baseline

The initial baseline covers classical thresholding and watershed, Cellpose 3, Cellpose-SAM, StarDist 2D, InstanSeg, and the specialized NAGINI-3D surface model.
These tools cover routine 2D cell/nucleus segmentation, classical reproducible baselines, and a distinct volumetric surface-analysis use case.

## Next segmentation candidates

The next integrations should be evaluated in this order:

1. BioImage.IO model execution for interoperable published models.
2. ilastik headless execution for trainable classical pixel classification.
3. DeepCell Mesmer for tissue cell and nucleus segmentation.
4. micro-SAM for promptable microscopy segmentation.
5. nnU-Net v2 for user-trained volumetric biomedical models.

BiaPy is not an internal BioImageFlow abstraction.
It may later be useful as an optional training or batch-inference adapter, but its configuration layer does not remove the need for explicit native input, output, model, and provenance contracts.

## Tracking direction

Nearest-neighbor assignment remains the deterministic migration baseline, while LapTrack is the first lineage-capable integration.
btrack is the next candidate after the normalized one-parent lineage schema has production use.
Ultrack should be considered only when overlap-aware large-scale lineage workflows justify its richer native data model.

## Small next-step shortlist by package

The following are candidates, not commitments.
Each package should add at most one of these at a time and only with a maintained workflow.

- Common tools: Parquet table input/output with explicit schema and nullable-type preservation.
- IO tools: an optional Bio-Formats fallback for unsupported proprietary microscopy files, followed by streaming pyramidal OME-Zarr conversion when workflows exceed memory.
- Segmentation tools: BioImage.IO execution first, then ilastik headless, Mesmer, micro-SAM, or nnU-Net v2 according to the workflow need described above.
- Measurement tools: label-neighborhood/contact graphs and masked channel-colocalization metrics with physical-unit support.
- Spot tools: evaluate Big-FISH only if a pinned current-Python environment and a dense-smFISH workflow justify it; the existing LoG, DoG, local-maxima, assignment, and QC tools remain the baseline.
- Tracking tools: btrack is the next alternative backend after LapTrack's normalized lineage contract is exercised on public data.
- Restoration tools: add BioImage.IO restoration-model execution rather than another framework-specific wrapper; keep CAREamics as the native training/checkpoint path.
- Phasor tools: multi-harmonic selection and phasor-region statistics are the next useful operations after the initial conversion, calibration, filtering, and lifetime path.
- SAIRPICO tools: prioritize cross-platform binary availability and acceptance data over adding another algorithm wrapper.

This shortlist intentionally leaves visualization, training dashboards, and interactive annotation sessions outside worker tools unless BioImageFlow first gains an explicit stateful-session contract.

## Package-wide admission criteria

A new integration must expose a stable real-world contract, use an isolated and reproducibly pinned runtime, validate inputs and outputs, preserve model/data provenance, provide fast contract tests and real-runtime acceptance coverage, and ship with a maintained workflow using licensed checksum-pinned public data.
Wrappers that only reproduce a toy command or hide an upstream library's important semantics should not be added.
