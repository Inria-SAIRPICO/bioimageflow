Specialized Tool Workflows
==========================

These lightweight tool packages ship with matching synthetic workflows. The
default test path avoids public data and heavyweight optional libraries.

.. image:: specialized_tool_workflows.svg
   :alt: Specialized tool workflow overview

Spot Tools
----------

``bioimageflow-spot-tools`` provides:

* ``DetectSpots`` for LoG/DoG/local maxima puncta detection.
* ``AssignSpotsToLabels`` for assigning spot coordinates to label images.
* ``SpotSummary`` for per-label spot count and intensity summaries.

Big-FISH is intentionally optional and reserved for evaluation runs.

Restoration Tools
-----------------

``bioimageflow-restoration-tools`` provides:

* ``RestoreImage`` for a scikit-image restoration baseline, with a NumPy fallback.
* ``BenchmarkRestoration`` for a synthetic blur/noise benchmark that writes metrics.

Tracking Tools
--------------

``bioimageflow-tracking-tools`` provides:

* ``LabelsToObjects`` for object centroid extraction from label images.
* ``LinkObjects`` for lightweight nearest-neighbor linking.
* ``TrackMetrics`` for track length, displacement, speed, and area summaries.

btrack and LapTrack remain optional for heavier evaluation environments.

Example Workflows
-----------------

The synthetic examples live under ``example-workflows``:

* ``puncta_analysis``
* ``restoration_benchmark``
* ``tracking_analysis``
