Workflow Catalog
================

These are the public example workflows. Each workflow has a deterministic normal-CI fixture and a contract in ``example-workflows/<workflow>/data_manifest.yml`` and ``expected_outputs.yml``. Public datasets, model runtimes, and external binaries are opt-in and marked with ``public_data``, ``model_runtime``, ``external_binary``, or package-specific markers.

Tests
  Deterministic workflow tests run in normal CI with generated fixtures. Public data, model runtime, and external binary checks require explicit markers and ``--run-complete``.

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

Analysis question
  How many FOLS2 and CSF1R FISH marker spots overlap each segmented nucleus?

Data
  Public validation uses Cell Image Library records ``13432``, ``13434``, ``13436``, and ``13438``. Normal tests use a generated CYX fixture.

Command
  ``python example-workflows/fish_analysis/workflow.py``

Expected outputs
  Per-image FOLS2 and CSF1R spot-per-nucleus summaries from the canonical ``MarkerSpotAnalysis`` sub-workflow branches.

Interpretation
  Higher average marker spots per nucleus indicate stronger marker signal in segmented nuclei for that image.

parameter_space_exploration
---------------------------

Analysis question
  How sensitive are ATLAS spot counts and masks to a small parameter grid?

Data
  Normal tests use a generated spot image. Public-data validation can reuse a FISH marker channel crop.

Command
  ``python example-workflows/parameter_space_exploration/workflow.py``

Expected outputs
  One mask per image and parameter combination plus a mosaic. The row count is ``n_images * n_param_combinations``.

Interpretation
  Compare spot counts and masks across sensitivity and size settings before selecting parameters for a marker workflow.

bbbc038_segmentation_benchmark
------------------------------

Analysis question
  How do planned nuclei segmentation methods compare against reference labels?

Data
  Public validation uses a named subset from the Broad Bioimage Benchmark Collection BBBC038 ``stage1_train``. Normal tests use a generated image/reference pair.

Command
  ``python example-workflows/bbbc038_segmentation_benchmark/workflow.py``

Expected outputs
  One metric row per method: Cellpose v3, Cellpose-SAM, StarDist, and a classical threshold baseline.

Interpretation
  Foreground IoU and Dice summarize agreement with reference nuclei masks.

cell_counting_phenotyping
-------------------------

Analysis question
  How many cells are in an image, and what are their basic region phenotypes?

Data
  Normal tests use a generated BBBC038-style crop. Public validation can reuse a BBBC038 crop.

Command
  ``python example-workflows/cell_counting_phenotyping/workflow.py``

Expected outputs
  A per-image table with object count, mean area, total area, and centroid summaries.

Interpretation
  Use the per-image phenotype row to compare cell density and size across images.

low_snr_restoration
-------------------

Analysis question
  Does CAREamics-style restoration improve a low-SNR microscopy image?

Data
  Normal tests use generated clean/degraded images. Real CAREamics checkpoints are opt-in model-runtime validation.

Command
  ``python example-workflows/low_snr_restoration/workflow.py``

Expected outputs
  Clean, degraded, and restored image paths plus MSE, PSNR, and residual-noise metrics.

Interpretation
  A useful restoration should reduce MSE and increase PSNR relative to the degraded input on the pinned fixture.

sairpico_deconvolution
----------------------

Analysis question
  What does a SAIRPICO denoise and Richardson-Lucy deconvolution pipeline produce for a small microscopy crop?

Data
  Normal tests generate a tiny input and monkeypatch SAIRPICO commands. Real SAIRPICO binaries are opt-in.

Command
  ``python example-workflows/sairpico_deconvolution/workflow.py``

Expected outputs
  PSF, denoised image, deconvolved image, sharpness metrics, and residual-noise metrics.

Interpretation
  Sharpness and residual-noise metrics provide quick regression signals for deconvolution behavior.

live_cell_tracking
------------------

Analysis question
  What migration tracks and metrics do Ultrack and btrack adapters produce for a short 2D time series?

Data
  Normal tests use a generated TYX label movie. Public validation can use selected frames from a small Cell Tracking Challenge 2D dataset.

Command
  ``python example-workflows/live_cell_tracking/workflow.py``

Expected outputs
  Track tables and migration metrics for Ultrack and btrack adapters. Lineage outputs are intentionally out of scope.

Interpretation
  Track length, displacement, mean speed, and mean area summarize migration behavior without division analysis.
