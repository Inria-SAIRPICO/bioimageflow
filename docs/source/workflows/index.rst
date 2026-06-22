Workflow Catalog
================

These are the public example workflows. Each entry shows the goal, data source, command, and practical results a workflow author can expect. Use the documented input paths for quick local runs, and enable selected public-data and model-runtime checks for deeper validation.

.. raw:: html

   <pre class="mermaid">
   flowchart LR
     source[Data source]:::source --> processing[Workflow tools]:::process
     processing --> metrics[Metrics tables]:::metric
     processing --> artifacts[Image artifacts]:::artifact
     classDef source fill:#e7f0ff,stroke:#4b73b9,color:#1b2f55
     classDef process fill:#edf8ef,stroke:#4d8f5b,color:#173d20
     classDef metric fill:#fff4d6,stroke:#a77a18,color:#4a3200
     classDef artifact fill:#f3eafd,stroke:#7d57a8,color:#332047
   </pre>

fish_analysis
-------------

Goal
  How many FOLS2 and CSF1R FISH marker spots overlap each segmented nucleus?

Data
  Public validation uses Cell Image Library records ``13432``, ``13434``, ``13436``, and ``13438``. Normal tests verify graph and output contracts without downloading the CIL files.

Command
  ``python example-workflows/fish_analysis/workflow.py``

How it works
  The workflow downloads four Cell Image Library FISH images, extracts the nuclei channel before Cellpose v3 segmentation, runs the same ``MarkerSpotAnalysis`` sub-workflow for FOLS2 and CSF1R, and summarizes marker spots per nucleus.

Results
  Per-image FOLS2 and CSF1R spot summaries, nuclei labels, and marker-overlap tables.

Interpretation
  Higher average marker spots per nucleus indicate stronger marker signal in segmented nuclei for that image.

parameter_space_exploration
---------------------------

Goal
  How sensitive are ATLAS spot counts and masks to a small parameter grid?

Data
  Use a FISH marker-channel crop, such as a FOLS2 or CSF1R channel extracted from a CIL FISH image.

Command
  ``python example-workflows/parameter_space_exploration/workflow.py``

How it works
  The workflow builds a grid of ATLAS spot detection parameters, applies every parameter set to FISH marker-channel images, computes spot counts and quality measurements, and renders a mosaic for visual comparison.

Results
  A parameter-results table, spot masks for each parameter combination, and a mosaic preview.

Interpretation
  Compare spot counts and masks across sensitivity and size settings before selecting parameters for a marker workflow.

bbbc038_segmentation_benchmark
------------------------------

Goal
  How do planned nuclei segmentation methods compare against reference labels?

Data
  Use a named subset from the Broad Bioimage Benchmark Collection BBBC038 ``stage1_train`` with each sample's original ``images/`` and ``masks/`` folders.

Command
  ``python example-workflows/bbbc038_segmentation_benchmark/workflow.py --data-dir data/bbbc038_stage1_train_subset``

How it works
  The workflow lists BBBC038 sample folders, builds a reference label image from each sample's instance-mask files, prepares a 2D intensity image for segmentation, runs Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method as separate graph branches, then benchmarks each prediction against the same reference labels.

Results
  Predicted label images, overlays, and a benchmark metrics table with one row per method and image.

Interpretation
  Foreground IoU and Dice summarize agreement with reference nuclei masks.

cell_counting_phenotyping
-------------------------

Goal
  How many cells are in an image, and what are their basic region phenotypes?

Data
  Use a small nuclei crop from a BBBC038 sample or another 2D microscopy image where threshold segmentation is a reasonable default.

Command
  ``python example-workflows/cell_counting_phenotyping/workflow.py --input-image data/bbbc038_crop.tif``

How it works
  The workflow segments a small microscopy crop, measures object geometry, shape, and intensity features, and aggregates per-image phenotype summaries.

Results
  A label image, object feature table, and per-image count and phenotype summary.

Interpretation
  Use the per-image phenotype row to compare cell density and size across images.

low_snr_restoration
-------------------

Goal
  Does CAREamics-style restoration improve a low-SNR microscopy image?

Data
  Use a paired low-SNR microscopy crop, clean or high-SNR reference image, and CAREamics checkpoint. Model-runtime validation remains opt-in.

Command
  ``python example-workflows/low_snr_restoration/workflow.py --clean-image data/low_snr_clean_crop.tif --degraded-image data/low_snr_degraded_crop.tif --checkpoint models/careamics.ckpt``

How it works
  The workflow runs a CAREamics-facing restoration prediction step, compares the restored image with the noisy input and clean reference where available, and produces a side-by-side restoration assessment.

Results
  Restored image, metrics table, and comparison preview.

Interpretation
  A useful restoration should reduce MSE and increase PSNR relative to the degraded input on the same validation crop.

sairpico_deconvolution
----------------------

Goal
  What does a SAIRPICO denoise and Richardson-Lucy deconvolution pipeline produce for a small microscopy crop?

Data
  Use a supplied microscopy crop, for example a FISH crop from CIL. Real SAIRPICO binaries are opt-in.

Command
  ``python example-workflows/sairpico_deconvolution/workflow.py --input-image data/13432_fish_crop.tif``

How it works
  The workflow generates a PSF, runs SAIRPICO denoising, feeds the generated PSF into Richardson-Lucy deconvolution, and measures image sharpness and residual noise.

Results
  PSF image, denoised image, deconvolved image, and metrics table.

Interpretation
  Sharpness and residual-noise metrics provide quick regression signals for deconvolution behavior.

live_cell_tracking
------------------

Goal
  What migration tracks and metrics do Ultrack and btrack adapters produce for a short 2D label movie?

Data
  Use a TYX label movie from selected frames of a 2D Cell Tracking Challenge dataset.

Command
  ``python example-workflows/live_cell_tracking/workflow.py --label-image data/ctc_label_movie.tif``

How it works
  The workflow loads a short TYX label movie, runs Ultrack and btrack adapters, and computes basic migration metrics from each track table.

Results
  Track tables and migration metrics. Lineage and division analysis are intentionally out of scope.

Interpretation
  Track length, displacement, mean speed, and mean area summarize migration behavior without division analysis.
