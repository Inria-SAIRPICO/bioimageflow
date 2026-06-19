:orphan:

Specialized Tool Workflows
==========================

These lightweight tool packages ship with matching synthetic workflows. The
default test path avoids public data and heavyweight optional libraries.

Spot Tools
----------

Analysis question
  Which puncta are present in an intensity image, which segmented object owns
  each punctum, and what per-object summary should be reported?

``bioimageflow-spot-tools`` provides:

* ``DetectSpots`` for LoG/DoG/local maxima puncta detection.
* ``AssignSpotsToLabels`` for assigning spot coordinates to label images.
* ``SpotSummary`` for per-label spot count and intensity summaries.

Big-FISH is intentionally optional and reserved for evaluation runs.

Data and expected outputs
  The normal example uses generated 2D puncta and label images. Expected
  outputs are spot-coordinate dataframe rows, assigned spot dataframe rows,
  per-label summary dataframe rows, and deterministic label counts.

Test coverage
  ``tests/specialized_tool_workflows/test_example_workflows.py`` executes the
  package example workflow and verifies the summary dataframe is non-empty.

Restoration Tools
-----------------

Analysis question
  Does a restoration baseline improve a degraded synthetic microscopy-like
  image according to reproducible quality metrics?

``bioimageflow-restoration-tools`` provides:

* ``RestoreImage`` for a scikit-image restoration baseline, with a NumPy fallback.
* ``BenchmarkRestoration`` for a synthetic blur/noise benchmark that returns dataframe metrics.

Data and expected outputs
  The normal example uses generated fixed-seed data. Expected outputs are clean,
  degraded, restored images, dataframe metric columns, and higher restored PSNR
  than degraded PSNR for the default example.

Test coverage
  ``tests/specialized_tool_workflows/test_example_workflows.py`` executes the
  package example workflow and verifies the metric dataframe is non-empty.

Tracking Tools
--------------

Analysis question
  Can segmented objects in a time-lapse label stack be linked into stable
  tracks and summarized for quality control?

``bioimageflow-tracking-tools`` provides:

* ``LabelsToObjects`` for object centroid extraction from label images.
* ``LinkObjects`` for lightweight nearest-neighbor linking.
* ``TrackMetrics`` for track length, displacement, speed, and area summaries.

btrack and LapTrack remain optional for heavier evaluation environments.

Data and expected outputs
  The normal example uses a generated TYX label stack with two moving objects.
  Expected outputs are object dataframe rows, linked track dataframe rows,
  track-count metrics, and a deterministic mean track length.

Test coverage
  ``tests/specialized_tool_workflows/test_example_workflows.py`` executes the
  package example workflow and verifies the metrics dataframe is non-empty.

Example Workflows
-----------------

The synthetic examples live under ``example-workflows``:

* ``puncta_analysis``
* ``restoration_benchmark``
* ``tracking_analysis``
