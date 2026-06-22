Workflow Catalog
================

These are the public example workflows. Each entry shows the goal, data source, command, and practical results a workflow author can expect. Small local fixtures keep the examples quick to try, while selected public-data and model-runtime checks are available for deeper validation.

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
  Public validation uses Cell Image Library records ``13432``, ``13434``, ``13436``, and ``13438``. Normal tests use a generated CYX fixture.

Command
  ``python example-workflows/fish_analysis/workflow.py``

How it works
  The workflow downloads four Cell Image Library FISH images, extracts the nuclei channel before Cellpose v3 segmentation, runs the same ``MarkerSpotAnalysis`` sub-workflow for FOLS2 and CSF1R, and summarizes marker spots per nucleus.

Results
  Per-nucleus and per-image FOLS2 and CSF1R spot summaries, nuclei labels, marker spot tables, and preview overlays.

Interpretation
  Higher average marker spots per nucleus indicate stronger marker signal in segmented nuclei for that image.

parameter_space_exploration
---------------------------

Goal
  How sensitive are ATLAS spot counts and masks to a small parameter grid?

Data
  Normal tests use a generated spot image. Public-data validation can reuse a FISH marker channel crop.

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
  Public validation uses a named subset from the Broad Bioimage Benchmark Collection BBBC038 ``stage1_train``. Normal tests use a generated image/reference pair.

Command
  ``python example-workflows/bbbc038_segmentation_benchmark/workflow.py``

How it works
  The workflow prepares BBBC038-style images and reference masks, runs Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold method on the same images, then compares each prediction to the reference labels.

Results
  Predicted label images, overlays, and a benchmark metrics table with one row per method and image.

Interpretation
  Foreground IoU and Dice summarize agreement with reference nuclei masks.

cell_counting_phenotyping
-------------------------

Goal
  How many cells are in an image, and what are their basic region phenotypes?

Data
  Normal tests use a generated BBBC038-style crop. Public validation can reuse a BBBC038 crop.

Command
  ``python example-workflows/cell_counting_phenotyping/workflow.py``

How it works
  The workflow segments a small microscopy crop, measures object region properties, and aggregates per-image phenotype summaries.

Results
  A label image, object feature table, and per-image count and phenotype summary.

Interpretation
  Use the per-image phenotype row to compare cell density and size across images.

low_snr_restoration
-------------------

Goal
  Does CAREamics-style restoration improve a low-SNR microscopy image?

Data
  Normal tests use generated clean/degraded images. Real CAREamics checkpoints are opt-in model-runtime validation.

Command
  ``python example-workflows/low_snr_restoration/workflow.py``

How it works
  The workflow runs a CAREamics-facing restoration prediction step, compares the restored image with the noisy input and clean reference where available, and produces a side-by-side restoration assessment.

Results
  Restored image, metrics table, and comparison preview.

Interpretation
  A useful restoration should reduce MSE and increase PSNR relative to the degraded input on the pinned fixture.

sairpico_deconvolution
----------------------

Goal
  What does a SAIRPICO denoise and Richardson-Lucy deconvolution pipeline produce for a small microscopy crop?

Data
  Normal tests generate a tiny input and monkeypatch SAIRPICO commands. Real SAIRPICO binaries are opt-in.

Command
  ``python example-workflows/sairpico_deconvolution/workflow.py``

How it works
  The workflow generates a PSF, runs SAIRPICO denoising and Richardson-Lucy deconvolution, and measures image sharpness and residual noise.

Results
  PSF image, denoised image, deconvolved image, metrics table, and preview.

Interpretation
  Sharpness and residual-noise metrics provide quick regression signals for deconvolution behavior.

live_cell_tracking
------------------

Goal
  What migration tracks and metrics do Ultrack and btrack adapters produce for a short 2D time series?

Data
  Normal tests use a generated TYX label movie. Public validation can use selected frames from a small Cell Tracking Challenge 2D dataset.

Command
  ``python example-workflows/live_cell_tracking/workflow.py``

How it works
  The workflow loads a short 2D time series or label movie, runs Ultrack and btrack adapters, and computes basic migration metrics from each track table.

Results
  Track tables, migration metrics, and overlay frames. Lineage and division analysis are intentionally out of scope.

Interpretation
  Track length, displacement, mean speed, and mean area summarize migration behavior without division analysis.
