# BioImageFlow Tool Package Strategy

This document turns the broad library-centric inventory in
`docs/tools_and_worklfows.md` into a workflow-first packaging plan. The goal is
not to integrate every attractive bioimage library. The goal is to define a
small set of optional tool packages that make real, demonstrable workflows
possible, with public data and measurable expected outputs.

## Starting Point

BioImageFlow is designed for optional tool packages:

- `bioimageflow-core` is installed in every worker environment and stays small.
- `bioimageflow` orchestrates workflows in the main process.
- `ProcessingTool` classes declare isolated `EnvironmentSpec` objects.
- Tool packages are versioned Python packages loaded through the tool store.
- Tool packages must use relative imports internally.
- Heavy dependencies should be imported inside `process_row` or
  `process_batch`, not at module import time.

The current `bioimageflow_common_tools` package already contains source,
merge, conversion, channel extraction, connected components, spot detection,
Cellpose, StarDist, label overlap, and visualization tools. That was useful for
bootstrapping, but it mixes basic glue tools with domain tools that have heavy
dependencies and independent maintenance schedules.

## Selection Rules

A tool should be promoted into an optional package only if it satisfies most of
these criteria:

1. It supports at least one named workflow that BioImageFlow should demonstrate.
2. It has a public library, binary, model, or command-line tool that can be
   installed reproducibly.
3. It has public input data for validation, or it can be validated with
   synthetic data whose expected result is known.
4. Its outputs can be expressed with BioImageFlow types: images, labels,
   tables, coordinates, tracks, or metadata.
5. It is narrow enough to document as a process, not just as a library wrapper.
6. Its environment can be isolated cleanly with Wetlands.
7. Its maintenance cost is justified by workflow value.

This means entries such as "scikit-image" should not become one generic
`ScikitImageTool`. They should become concrete tools such as `GaussianSmooth`,
`OtsuThreshold`, `Watershed`, `RegionProperties`, or
`RichardsonLucyDeconvolution`, each with explicit input/output semantics.

## Proposed Package Map

### `bioimageflow-common-tools`

Purpose: small, stable graph and image-analysis primitives that are broadly
useful and cheap to maintain.

Core libraries:

- `bioimageflow`
- `bioimageflow-core`
- `numpy`
- `scipy`
- `scikit-image`
- `imageio`
- `tifffile`
- `Pillow`

Keep here:

- `Files`
- `Generate`
- `CrossJoin`, `JoinOnColumn`, `InnerJoin`, `Concat`, `Collect`
- `ConvertImage` for simple local conversions
- `ExtractChannel`
- `ConnectedComponents`
- `LabelOverlaps`
- `Mosaic`
- Simple `GENERAL_ENV` image operations when they are dependency-light:
  `GaussianSmooth`, `MedianFilter`, `OtsuThreshold`, `BinaryMorphology`,
  `DistanceTransform`, `Watershed`

Move or re-home later:

- `CellposeSAM` and `Cellpose3` should belong to
  `bioimageflow-segmentation-tools`.
- `StarDistSegmenter` should belong to `bioimageflow-segmentation-tools`.
- `Atlas` should eventually belong to `bioimageflow-spot-tools`, but can stay
  temporarily in common to keep the current FISH example working.

Backward compatibility is not a constraint for the example workflows in this
strategy. They should be updated to import tools from the canonical
domain-specific packages instead of relying on legacy forwarding from
`bioimageflow_common_tools`.

### `bioimageflow-sairpico-tools`

Purpose: tools built by the SAIRPICO team, preserving local scientific value
while isolating their legacy dependencies.

Core libraries and binaries:

- `sylvainprigent::simglib=0.1.2`
- `bioimageit::hotspot==1.0.0`
- `bioimageit::cimgdenoising==1.0.0`
- Python standard-library `subprocess` wrappers around the command-line tools

Source inventory:
`/Users/amasson/Travail/bioimageit/PyFlow/Tools/Sairpico`

Candidate tools:

| Tool | Process | Runtime dependency | Validation idea |
|------|---------|--------------------|-----------------|
| `GaussianPSF` | Generate a 3D Gaussian PSF | `sylvainprigent::simglib=0.1.2` | Output has requested shape; maximum near center; sum positive |
| `GibsonLanniPSF` | Generate a Gibson-Lanni PSF | `sylvainprigent::simglib=0.1.2` | Output has requested shape; physically plausible central peak |
| `RichardsonLucyDeconvolution` | 2D, slice-wise 2D, or 3D deconvolution | `sylvainprigent::simglib=0.1.2` | Synthetic blurred image recovers sharper object; public microscopy image improves spot sharpness |
| `WienerDeconvolution` | 2D, slice-wise 2D, or 3D Wiener deconvolution | `sylvainprigent::simglib=0.1.2` | Synthetic blur/noise benchmark improves PSNR or SSIM |
| `SpitfireDeconvolution` | SPITFIR(e) deconvolution | `sylvainprigent::simglib=0.1.2` | Synthetic blur/noise benchmark improves high-frequency contrast without exploding intensity |
| `MedianDenoising` | 2D, 3D, or 4D median filtering | `sylvainprigent::simglib=0.1.2` | Salt-and-pepper synthetic image has lower error after filtering |
| `CImgDenoising` | Patch, variational, and classical denoising methods | `bioimageit::cimgdenoising==1.0.0` | Public noisy microscopy data; output noise estimate decreases |
| `HotspotDetection` | Hotspot detection in 2D microscopy images | `bioimageit::hotspot==1.0.0` | Synthetic bright spots are detected; CIL FISH data produces sparse hotspot mask |

Packaging risks:

- `cimgdenoising` currently appears to be declared only for `osx-64` and
  `win-64` in the legacy tool metadata. Linux availability must be checked
  before this can be a default CI-tested package.
- The SAIRPICO tools depend on non-default conda channels
  (`sylvainprigent`, `bioimageit`). The package should document these channels
  explicitly and pin exact versions.
- The legacy `lambda` parameter name used in Python metadata must be renamed in
  BioImageFlow wrappers, for example to `regularization_lambda`.
- The package should expose process-specific classes, not a generic
  command-runner wrapper.

### `bioimageflow-io-tools`

Purpose: robust import/export and format normalization.

Core libraries and binaries:

- `bioio`
- selected `bioio-*` plugins, chosen from real workflow needs rather than
  installed all at once
- `tifffile`
- `imageio`
- `zarr`
- `ome-zarr`
- Bio-Formats / `bftools` for the optional Java converter
- SimpleITK for medical-image formats if this package keeps medical I/O

Candidate tools:

- `ReadImage`: read common image formats with `bioio` plus selected plugins.
- `ConvertImageFormat`: convert common image files, OME-TIFF, and lightweight
  OME-Zarr, with optional scene/channel/Z/T selection.
- `ConvertToOmeTiff`: convert an image file to OME-TIFF with explicit
  dimension order and metadata.
- `ConvertToOmeZarr`: convert an image file to chunked OME-Zarr.
- `SelectScene`: extract one scene from multi-scene files.
- `SelectTimepoint`, `SelectZRange`, `SelectChannel`: explicit dimension
  subsetting tools.
- `ConvertWithBioformats`: optional Java/Bio-Formats based converter for
  formats not covered by pure Python readers.
- `ReadMedicalImage`: SimpleITK-based NIfTI, NRRD, MHA, or DICOM-adjacent
  reader. This may also become its own medical-imaging package later.

Public validation data:

- Cell Image Library images, including CIL:13432.
- IDR / OME-Zarr public samples.
- Small local synthetic OME-TIFF fixtures for CI.

Expected results:

- Read tools preserve array shape, dimension order, physical pixel sizes where
  available, and channel metadata where supported.
- Conversion tools round-trip small fixtures without shape or dtype changes.

### `bioimageflow-segmentation-tools`

Purpose: cell, nucleus, and object segmentation with a small number of
well-supported methods.

Core libraries:

- `cellpose`
- `stardist`
- `tensorflow` only inside the StarDist environment when needed
- `scikit-image`
- `scipy`
- `numpy`

Candidate tools:

- `CellposeSegment`: Cellpose / Cellpose-SAM inference for nuclei, cells, and
  cyto models.
- `StarDist2DSegment`: pretrained 2D StarDist models for fluorescence nuclei
  and H&E nuclei.
- `ThresholdSegment`: Otsu, Li, Yen, Triangle, Sauvola, local thresholding.
- `WatershedSegment`: distance-transform and marker-based watershed.
- `PostprocessLabels`: remove small objects, fill holes, clear border,
  relabel sequentially.

Public validation data:

- BBBC038 / 2018 Data Science Bowl nuclei data, which provides images and
  instance masks.
- StarDist built-in test nuclei image can be used only for smoke tests, because
  it is bundled with the library rather than a workflow dataset.

Expected results:

- Instance label masks with `Semantic.LABEL`.
- Object counts in a plausible range for each validation image.
- For BBBC038, compute IoU-based matching metrics: precision, recall, F1, and
  panoptic quality where available.

### `bioimageflow-measurement-tools`

Purpose: convert labels and images into tables that downstream statistical
tools can consume.

Core libraries:

- `numpy`
- `pandas`
- `scikit-image`
- `scipy`
- `tifffile` or `imageio`

Candidate tools:

- `RegionProperties`: area, centroid, bounding box, eccentricity, perimeter,
  solidity, axis lengths.
- `IntensityProperties`: mean, min, max, integrated intensity per label and
  channel.
- `CountLabels`: object counts per image.
- `LabelAdjacency`: touching-neighbor graph for tissue or cell packing.
- `ColocalizationMetrics`: Pearson, Manders, overlap coefficient for channels
  or masked regions.
- `SummarizeTable`: group-by aggregation for workflow-level summaries.

Public validation data:

- BBBC038 labels and images.
- CIL FISH images after spot and nucleus segmentation.
- Synthetic labels with known areas, centroids, and overlaps.

Expected results:

- Tables with stable column names and one row per object or one row per image.
- Synthetic tests should assert exact measurements.
- Public workflow tests should assert non-empty results and biologically
  plausible value ranges.

### `bioimageflow-spot-tools`

Purpose: spot, puncta, and hotspot detection plus object-level assignment. This
package should be workflow-driven and should not be centered on Big-FISH by
default. Big-FISH is scientifically relevant, but its latest public release is
old enough that it should be evaluated before it becomes a maintenance
commitment.

Big-FISH adoption note:

- As checked on May 7, 2026, the Big-FISH GitHub page lists release `0.6.2`
  from April 25, 2022.
- The development documentation says Big-FISH is tested on Python 3.6, 3.7,
  3.8, and 3.9. That may still be acceptable in an isolated Wetlands
  environment, but it is a compatibility and maintenance risk.
- Big-FISH should therefore be integrated only after a pinned environment and a
  useful validation workflow are proven.

Core libraries and binaries:

- `scikit-image`
- `scipy`
- `numpy`
- current `Atlas` CLI dependency from `bioimageflow-common-tools`
- optional SAIRPICO `hotspot` bridge, if the team wants it outside
  `bioimageflow-sairpico-tools`
- optional `big-fish`, after Python compatibility and workflow validation are
  checked

Candidate tools:

- `LoGSpotDetection`: Laplacian-of-Gaussian spot detection using
  `scikit-image` or `scipy`.
- `DoGSpotDetection`: Difference-of-Gaussian spot detection.
- `LocalMaximaSpotDetection`: maxima detection with absolute or adaptive
  thresholds.
- `AtlasSpotDetection`: move or replace the current `Atlas` wrapper under spot
  package ownership once the validation workflow is stable.
- `HotSpotDetection`: either keep in `bioimageflow-sairpico-tools` only or add
  a spot-package bridge with explicit SAIRPICO ownership and tests.
- `AssignSpotsToLabels`: assign spot coordinates or spot labels to nuclei or
  cell labels.
- `SpotsPerObjectSummary`: per-image and per-object spot counts.
- `BigFishDetectSpots`: optional candidate, not first implementation.
- `BigFishDenseDecomposition`: optional candidate only if dense smFISH
  workflows need it.
- `BigFishSpotSNR`: optional candidate if Big-FISH is adopted.

Public validation data:

- CIL:13432, CIL:13434, CIL:13436, and CIL:13438 are already used by the
  example FISH workflow. They are public Cell Image Library images with FOLS2,
  CSF1R, and DAPI channels.
- For algorithmic spot-detection tests, synthetic spots with known coordinates
  are better than relying only on CIL images because CIL does not provide spot
  ground truth.

Expected results:

- Spot masks or coordinate tables.
- Nucleus labels.
- A final table with spots per nucleus and image-level averages.
- Synthetic tests should recover spots within a configurable pixel tolerance.

Package priority:

- Priority: specialized package, already part of the package split.
- The first FISH workflow can use `Atlas`, SAIRPICO `HotSpotDetection`, or the
  spot package's public-library tools while the package grows toward richer
  LoG/DoG/local-maxima coverage.
- Big-FISH should be kept in an "evaluate" state until it has a working pinned
  environment and a useful validation workflow.

### `bioimageflow-tracking-tools`

Purpose: link per-frame detections into trajectories and lineage tables.

Core libraries:

- `btrack`
- `laptrack`
- `trackpy`
- `numpy`
- `pandas`
- `scikit-image`
- possibly `ultrack` later, after a smaller tracker is integrated

Candidate tools:

- `LabelsToObjects`: convert time-indexed label images into object tables.
- `BTrackLink`: Bayesian tracking with optional lineage reconstruction.
- `LapTrackLink`: linear assignment based tracking.
- `TrackpyParticles`: particle tracking for small subcellular objects.
- `TrackMetrics`: displacement, speed, track duration, division count.
- `TracksToLabels`: render tracks or track IDs back into images.

Public validation data:

- Cell Tracking Challenge 2D+t datasets such as DIC-C2DH-HeLa or
  Fluo-N2DL-HeLa.

Expected results:

- Track table with `track_id`, `t`, `y`, `x`, optional `z`, parent track ID,
  and object label.
- Validation against provided annotations for a small training subset.
- Metrics: number of tracks, average duration, linking precision/recall where
  ground truth is available.

### `bioimageflow-restoration-tools`

Purpose: denoising, deconvolution, and learned restoration outside the
SAIRPICO-specific package.

Core libraries:

- `scikit-image`
- `scipy`
- `numpy`
- `careamics`
- `n2v` or the current Noise2Void successor used by the community
- `csbdeep` only if legacy CARE models need it

Candidate tools:

- `SkimageRichardsonLucy`: CPU Richardson-Lucy baseline.
- `N2VDenoise` and possibly `N2VTrain`: Noise2Void inference and training.
- `CAREamicsPredict` and possibly `CAREamicsTrain`: CARE / Noise2Void style
  restoration using CAREamics.
- `EstimateNoise`: estimate image noise level for reporting and parameter
  selection.

Public validation data:

- ZeroCostDL4Mic Noise2Void 2D and 3D datasets.
- ZeroCostDL4Mic CARE 2D and 3D paired low/high SNR datasets.
- Synthetic blur/noise fixtures with known clean images.

Expected results:

- For paired data, PSNR or SSIM improves against the high-SNR target.
- For self-supervised data, noise estimate decreases while structural metrics
  remain stable.

Relationship to `bioimageflow-sairpico-tools`:

- SAIRPICO wrappers stay in their own package because they use SAIRPICO-owned
  binaries and conda channels.
- General restoration wrappers belong here because they are public-library
  based and may be useful outside SAIRPICO workflows.

### Specialized Growth Packages

These packages now exist with synthetic tests and baseline P0 tools. They should
grow from proven workflow needs, not from library cataloguing:

- `bioimageflow-spot-tools`: `scikit-image`, `scipy`, `Atlas`, optional
  `big-fish`. Useful for FISH and puncta workflows. Growth should focus first
  on robust public-library spot tables, rendering, QC, and assignment before a
  Big-FISH commitment.
- `bioimageflow-restoration-tools`: `scikit-image`, `scipy`, `numpy`,
  `careamics`, `n2v`, `csbdeep`. Current baselines are public-library tools;
  learned restoration should wait for public-data benchmarks and model
  lifecycle rules.
- `bioimageflow-tracking-tools`: `btrack`, `laptrack`, `trackpy`, possibly
  `ultrack`. Current baselines cover table validation, rendering, summaries,
  and QC. Algorithmic linking should grow only when time-lapse public ground
  truth and metrics are clear.
- `bioimageflow-registration-tools`: `pystackreg`, SimpleITK, ITKElastix,
  ANTsPy. Important for time-lapse, multimodal, and atlas workflows.
- `bioimageflow-stitching-tools`: ASHLAR, BigStitcher / ImageJ integration,
  possibly `aicsimageio` or `bioio` metadata helpers.
- `bioimageflow-spatial-omics-tools`: Squidpy, AnnData, spatialdata,
  scikit-learn, networkx.
- `bioimageflow-gpu-tools`: pyclesperanto, cuCIM, APOC. GPU/OpenCL/CUDA
  availability makes CI and user support more expensive.
- `bioimageflow-deep-segmentation-tools`: nnU-Net, Micro-SAM, nnInteractive,
  PlantSeg, Biom3d. These are powerful but model/data/GPU heavy and should be
  integrated only after the smaller segmentation package is stable.
- `bioimageflow-imagej-tools`: PyImageJ wrappers for Fiji plugins. Useful, but
  Java/ImageJ lifecycle management should be isolated from the main package set.

## Priority Workflows

These workflows should drive the first implementation choices. They are not
chosen only because they are common bioimage tasks. They are chosen because
they demonstrate BioImageFlow's core value: graph composition, package
isolation, typed image outputs, tabular outputs, caching, and reproducible
workflow-level results.

| Priority | Workflow | Packages needed | Public data | Expected output |
|----------|----------|-----------------|-------------|-----------------|
| 1 | FISH spot counting per nucleus | common, io, segmentation, measurement, optional sairpico | CIL:13432/13434/13436/13438 | Per-image and per-nucleus tables with spot counts |
| 1 | Nucleus segmentation benchmark | common, io, segmentation, measurement | BBBC038 | Label masks and IoU/F1/PQ metrics against masks |
| 1 | SAIRPICO restoration smoke workflow | common, sairpico, measurement | Synthetic blur/noise plus CIL FISH crop | Restored image, PSF output, sharpness/noise metrics |
| 2 | OME-TIFF and OME-Zarr normalization | common, io | CIL and IDR/OME-Zarr public samples | Shape, dtype, dimension-order, and metadata round-trip report |

### FISH spot counting per nucleus

Why it is valuable:

- It matches a real analysis need: quantify gene-marker spots inside nuclei.
- It is already close to the current example workflow, so it gives immediate
  continuity.
- It demonstrates branching graphs: one image splits into several channel
  branches, then branches rejoin for spatial assignment and summary.
- It produces both image artifacts and final tables, which is important for
  GUIs and provenance.
- It avoids making Big-FISH an initial dependency. The first version can use
  `Atlas`, a simple LoG detector, or SAIRPICO `HotSpotDetection`.

Candidate graph:

```text
Download or Files
  -> ReadImage / ConvertImage
  -> ExtractChannel(FOLS2) -> SpotDetection -> ConnectedComponents
  -> ExtractChannel(CSF1R) -> SpotDetection -> ConnectedComponents
  -> ExtractChannel(DAPI)  -> CellposeSegment or ThresholdSegment
  -> LabelOverlaps(FOLS2 labels, nuclei labels)
  -> LabelOverlaps(CSF1R labels, nuclei labels)
  -> RegionProperties / CountLabels / SummarizeTable
```

Public data:

- CIL:13432, CIL:13434, CIL:13436, CIL:13438.

Expected outputs:

- Per-image table: image ID, nucleus count, FOLS2 spot count, CSF1R spot count,
  mean FOLS2 spots per nucleus, mean CSF1R spots per nucleus.
- Optional per-nucleus table: nucleus label, area, centroid, FOLS2 count, CSF1R
  count.
- Intermediate masks: spot masks, spot labels, nuclei labels, optional mosaic.

Validation:

- CIL smoke tests assert non-empty nuclei labels, sparse spot masks, and stable
  output columns.
- Synthetic spot fixtures assert coordinate recovery within a pixel tolerance.
- The workflow should be useful even if exact biological counts change when a
  detector is improved.

### Nucleus segmentation benchmark

Why it is valuable:

- Segmentation is one of the most common entry points into bioimage analysis.
- Public ground truth exists, so this can be a real benchmark rather than only
  a smoke test.
- It exercises model-based tools, classical fallback tools, measurement tools,
  and workflow comparison.

Candidate graph:

```text
DownloadBBBC038 / Files
  -> ReadImage
  -> CellposeSegment
  -> StarDist2DSegment
  -> ThresholdSegment + WatershedSegment
  -> PostprocessLabels
  -> RegionProperties
  -> MatchLabelsToGroundTruth
  -> SummarizeBenchmark
```

Public data:

- BBBC038 / 2018 Data Science Bowl nuclei dataset.

Expected outputs:

- One label image per method and input image.
- Per-object measurements for predicted labels.
- Per-method metrics: object count, matched-object precision, recall, F1,
  mean IoU, and panoptic quality where implemented.
- A comparison table that makes tradeoffs visible: classical methods are fast
  and lightweight; Cellpose and StarDist may perform better but need heavier
  environments.

Validation:

- Exact synthetic-label tests for measurement code.
- Public-data benchmark tests on a small fixed subset with metric tolerances.
- Schema tests for all segmentation tools even when heavy environments are not
  installed.

### SAIRPICO restoration smoke workflow

Why it is valuable:

- It justifies the dedicated SAIRPICO package with tools that are not generic
  wrappers around common Python libraries.
- It exercises CLI-based `ProcessingTool` wrappers and non-default conda
  channels.
- It demonstrates environment isolation: SAIRPICO binaries can live in their
  own Wetlands environments without affecting Cellpose, StarDist, or BioIO.
- It can be validated with synthetic data, so it does not depend entirely on
  subjective visual inspection.

Candidate graph:

```text
GenerateSyntheticObject
  -> GaussianPSF or GibsonLanniPSF
  -> BlurAndAddNoise
  -> RichardsonLucyDeconvolution
  -> WienerDeconvolution
  -> SpitfireDeconvolution
  -> Region/Intensity/SharpnessMetrics
  -> SummarizeTable
```

Optional public-image branch:

```text
CIL crop
  -> MedianDenoising or CImgDenoising
  -> HotSpotDetection
  -> CountLabels / SummarizeTable
```

Expected outputs:

- Generated PSF with expected shape and central peak.
- Restored image for each method.
- Metrics table with PSNR or SSIM for synthetic paired data, plus sharpness and
  noise estimates.
- CLI execution logs and environment hashes through normal BioImageFlow
  provenance.

Validation:

- Synthetic blur/noise benchmark should improve PSNR or SSIM against the known
  clean image.
- PSF tests should assert shape, dtype, positive sum, and central maximum.
- Public-image smoke tests should assert successful execution and plausible
  output intensity range.

### OME-TIFF and OME-Zarr normalization

Why it is valuable:

- Many workflows fail before analysis because input formats, scenes, channels,
  and dimension order are ambiguous.
- This workflow demonstrates BioImageFlow as a reproducible ingestion layer,
  not only an analysis runner.
- It creates reusable fixtures for downstream workflow tests.

Candidate graph:

```text
Files or DownloadPublicImage
  -> ReadImage
  -> SelectScene / SelectChannel / SelectTimepoint / SelectZRange
  -> ConvertToOmeTiff
  -> ConvertToOmeZarr
  -> ReadImage
  -> CompareImageMetadata
```

Public data:

- CIL images for simple TIFF-like inputs.
- IDR / public OME-Zarr samples for chunked and multiscale data.
- Small synthetic OME-TIFF fixtures committed to tests.

Expected outputs:

- Normalized OME-TIFF or OME-Zarr files.
- Metadata report: shape, dtype, dimension order, channel names when present,
  physical pixel size when present, and chunk layout for Zarr.
- Round-trip comparison table.

Validation:

- Synthetic fixtures assert exact shape, dtype, and dimension order.
- Public-data smoke tests assert stable readable outputs without committing
  large data to the repository.

## Later Workflows

Later workflows should use the later packages. They are valuable, but they have
more dependency, data, or validation risk than the initial workflows.

| Workflow | Packages needed | Public data | Why later | Expected output |
|----------|-----------------|-------------|-----------|-----------------|
| Denoising/restoration benchmark | restoration, measurement | ZeroCostDL4Mic N2V/CARE data | model management and training/inference choices | Restored images and PSNR/SSIM/noise metrics |
| Timelapse cell tracking | segmentation, tracking, measurement | Cell Tracking Challenge datasets | requires time-lapse ground truth and tracking metrics | Track tables, lineage links, tracking scores |
| Registration and drift correction | registration, measurement | public time-lapse or multimodal microscopy pairs | parameter choices vary strongly by modality | registered images, transform files, alignment metrics |
| Whole-slide or multiplex stitching | stitching, io, measurement | ASHLAR/CyCIF or public tiled examples | large data and Java/toolchain issues | stitched mosaic and tile-position QC |
| Spatial-omics neighborhood analysis | spatial-omics, measurement | public Visium or spatialdata examples | data model choices should settle first | neighborhood graphs and per-cell spatial statistics |
| GPU accelerated image processing | gpu, common | synthetic and public microscopy fixtures | hardware availability complicates CI | CPU/GPU parity metrics and speed reports |
| Deep interactive or foundation-model segmentation | deep-segmentation, measurement | public 3D or interactive segmentation benchmarks | model size and GPU expectations | labels, prompt metadata, quality metrics |
| ImageJ/Fiji bridge workflow | imagej, io | public ImageJ example data | Java lifecycle management should be isolated | Fiji plugin outputs and reproducible parameter record |

Examples:

- **Denoising/restoration benchmark:** use paired low/high SNR data where
  possible. The workflow should compare a CPU baseline such as
  `SkimageRichardsonLucy` or simple denoising against learned restoration.
- **Timelapse tracking:** segment each frame, convert labels to object tables,
  link objects with btrack or LapTrack, then compute speed, displacement,
  duration, and division metrics.
- **Registration and drift correction:** register a moving image stack to a
  reference frame, save transforms, and report pre/post alignment metrics such
  as normalized cross-correlation or landmark distance.
- **Whole-slide or multiplex stitching:** read tiles plus metadata, estimate or
  refine tile positions, write a stitched image, and report tile overlap QC.
- **Spatial-omics neighborhood analysis:** combine label-derived cell tables
  with spatial omics measurements, build neighborhood graphs, and compute
  enrichment or co-occurrence statistics.

## Current Implementation and Growth Plan

### Stabilized Package Boundary

1. `bioimageflow-common-tools` is focused on glue tools, table operations,
   simple channel extraction, connected components, label overlaps, mosaics,
   and legacy module-level wrappers that existing workflows still import.
2. `bioimageflow-io-tools` owns the lightweight reader, dimension-selection,
   OME-TIFF converter, and OME-Zarr converter.
3. `bioimageflow-sairpico-tools` owns wrappers for the SAIRPICO legacy command
   line tools.
4. `bioimageflow-segmentation-tools` owns Cellpose, StarDist, threshold,
   watershed, and label-postprocessing tools.
5. `bioimageflow-measurement-tools` owns region, intensity, count, table
   summary, and label benchmark measurements.
6. `bioimageflow-spot-tools`, `bioimageflow-restoration-tools`, and
   `bioimageflow-tracking-tools` exist as specialized packages with synthetic
   demo workflows and package-local tests.
7. `Atlas` remains a legacy module-level common-tools wrapper for existing FISH
   examples; the growth target is to migrate or wrap that behavior under
   `bioimageflow-spot-tools`.

### Demonstrable Workflows

1. Update the existing FISH example so it uses package boundaries explicitly
   without requiring a Big-FISH-based package.
2. Add a BBBC038 segmentation benchmark workflow.
3. Add a SAIRPICO deconvolution smoke workflow with synthetic data.
4. Add an OME-TIFF / OME-Zarr normalization workflow.
5. Put workflow data downloads behind explicit scripts or tools, never commit
   large downloaded data into the tool packages.
6. Record expected metrics in small JSON files so regression tests can compare
   outputs within tolerances.

### Expand cautiously

1. Grow `bioimageflow-spot-tools` from LoG/DoG/local maxima toward Atlas and
   optional Big-FISH evaluation only when workflows justify the maintenance
   cost.
2. Grow `bioimageflow-restoration-tools` from synthetic scikit-image baselines
   toward public-data restoration benchmarks before adding heavier algorithms.
3. Grow `bioimageflow-tracking-tools` from nearest-neighbor linking toward
   btrack or LapTrack only when the demo data and expected metrics are clear.
4. Defer GPU, Java/ImageJ, stitching, and large foundation-model packages until
   the packaging and validation pattern is proven.

## Validation Standard

Every new tool package should include four validation layers:

1. **Schema tests:** import classes, serialize input/output schemas, and verify
   BioImageFlow type metadata. These tests should not require heavy runtime
   dependencies.
2. **Synthetic tests:** run the tool on tiny generated data with known expected
   outputs. These should be fast enough for CI when dependencies are available.
3. **Public-data smoke workflows:** download a small public dataset or crop,
   run a complete workflow, and assert stable output structure and broad metric
   ranges. These may be marked as optional or slow.
4. **Benchmark workflows:** compare against public ground truth when available.
   These should produce metrics, not just files.

Recommended output records:

- Tool package name and version.
- Runtime environment dependency hash.
- Public data URL and checksum.
- Workflow parameter JSON.
- Output file paths.
- Summary metrics table.

## Practical Conventions

Package layout:

```text
packages/bioimageflow-sairpico-tools/
  pyproject.toml
  bioimageflow_sairpico_tools/
    __init__.py
    environments.py
    psf.py
    deconvolution.py
    denoising.py
    detection.py
  tests/
    test_schema.py
    test_synthetic_smoke.py
```

Naming:

- Package names should be distribution names with hyphens:
  `bioimageflow-sairpico-tools`.
- Import names should use underscores:
  `bioimageflow_sairpico_tools`.
- Tool class names should describe the process:
  `RichardsonLucyDeconvolution`, not `SimglibTool`.
- User-facing `display_name` can keep familiar names:
  "Richardson-Lucy Deconvolution".

Environment design:

- Use `GENERAL_ENV` for tools that only need numpy, scipy, scikit-image,
  imageio, tifffile, or Pillow.
- Use one shared environment per heavy dependency family:
  `bioio`, `cellpose`, `stardist`, `sairpico-simglib`,
  `sairpico-hotspot`, `sairpico-cimgdenoising`.
- Treat `bigfish` as an optional evaluation environment, not as an initial
  dependency.
- Pin versions for workflow packages. Avoid unbounded dependency ranges in
  tool environments.

Documentation:

- Each package should have a README listing tools by process, not by upstream
  library.
- Each tool should document input semantics, output semantics, and one minimal
  workflow example.
- Each workflow should state the public dataset, expected output, and metric
  used for validation.

## Decision Summary

Build around workflows, not libraries.

Current core packages are:

1. `bioimageflow-common-tools`
2. `bioimageflow-io-tools`
3. `bioimageflow-sairpico-tools`
4. `bioimageflow-segmentation-tools`
5. `bioimageflow-measurement-tools`

The first workflows to make real should be:

1. FISH spot counting per nucleus on public CIL images, using existing Atlas,
   SAIRPICO HotSpot, and the implemented spot table/rendering/QC package tools.
2. Nucleus segmentation benchmark on BBBC038.
3. SAIRPICO deconvolution/denoising smoke workflow on synthetic and public
   microscopy data.
4. OME-TIFF and OME-Zarr normalization on public and synthetic fixtures.

This gives BioImageFlow a maintainable package story: each optional package has
a reason to exist, each tool has a concrete process, and each workflow can be
tested against public or reproducible data.

## Public References

- BioImageFlow architecture and tool package loading: `specs.md`
- Existing tool inventory: `docs/tools_and_worklfows.md`
- SAIRPICO legacy tools:
  `/Users/amasson/Travail/bioimageit/PyFlow/Tools/Sairpico`
- Cell Image Library FISH example, CIL:13432:
  https://www.cellimagelibrary.org/images/13432
- CIL data URLs used by the current FISH workflow:
  https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif,
  https://cildata.crbs.ucsd.edu/media/images/13434/13434.tif,
  https://cildata.crbs.ucsd.edu/media/images/13436/13436.tif,
  https://cildata.crbs.ucsd.edu/media/images/13438/13438.tif
- BBBC038 nuclei segmentation dataset:
  https://bbbc.broadinstitute.org/BBBC038
- Broad Bioimage Benchmark Collection:
  https://bbbc.broadinstitute.org/
- Cell Tracking Challenge datasets:
  https://celltrackingchallenge.net/datasets/
- Cell Tracking Challenge 2D datasets:
  https://celltrackingchallenge.net/2d-datasets/
- ZeroCostDL4Mic Noise2Void 2D dataset:
  https://zenodo.org/records/3713315
- ZeroCostDL4Mic CARE 2D dataset:
  https://zenodo.org/records/3713330
- Public OME-Zarr / IDR sample data:
  https://ome.github.io/www.openmicroscopy.org/2020/11/04/zarr-data.html
- BioIO documentation:
  https://bioio-devs.github.io/bioio/
- Cellpose documentation:
  https://cellpose.readthedocs.io/
- StarDist package documentation:
  https://pypi.org/project/stardist/
- Big-FISH GitHub repository:
  https://github.com/fish-quant/big-fish
- Big-FISH documentation:
  https://big-fish.readthedocs.io/
- btrack documentation:
  https://btrack.readthedocs.io/
