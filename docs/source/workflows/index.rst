Workflow Tutorials
==================

BioImageFlow's public examples are workflow-level tutorials.
Each page below introduces a real analysis pattern, shows the data layout expected by the workflow, and points to the files a user should inspect after a run.

The examples are deliberately scoped so the graph can be read and adapted, while the real-data paths point to established microscopy data sources such as the Cell Image Library, BBBC038, and the Cell Tracking Challenge.

.. toctree::
   :maxdepth: 1

   fish_analysis
   parameter_space_exploration
   bbbc038_segmentation_benchmark
   cell_counting_phenotyping
   low_snr_restoration
   sairpico_deconvolution
   live_cell_tracking

Start with :doc:`fish_analysis` if you want to learn BioImageFlow sub-workflows.
Use :doc:`bbbc038_segmentation_benchmark` when you need a segmentation-method comparison, and :doc:`cell_counting_phenotyping` when you need a compact segment-and-measure workflow.
The remaining tutorials cover spot-detection parameter sweeps, restoration, deconvolution, and migration tracking.
