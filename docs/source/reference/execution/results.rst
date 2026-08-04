Portable Result Export
======================

BioImageFlow exports one run-specific immutable result bundle for local submitted, remote submitted, and attached execution.
The bundle contains a versioned return manifest, a content manifest, and every record-owned or return-owned asset needed to interpret the public return.

Submitted execution
-------------------

Local and remote handles use the same destination-shaped API:

.. code-block:: python

   exported = run.export_result(downloads / run.id)

Local submission builds from retained storage; remote submission downloads through the bounded transport.
Both write a private sibling, verify the manifest and every entry digest, and atomically rename it into place.
The destination parent must already be a real non-symlink directory.

An existing destination is accepted only when it is the complete verified bundle for the same run and expected bundle digest.
This makes repeated export idempotent while rejecting self-consistent but different, partial, corrupt, foreign, or symlinked destinations.
The first local export durably retains the expected bundle digest with the run, so a later call can verify an already installed destination even if source assets were subsequently pruned.
``WorkflowRun.load_result()`` is a distinct local-only operation that reads the retained return without creating a portable bundle.

Asset ownership
---------------

Record-owned and return-owned assets are copied beneath the destination and returned path values are rehydrated to those local files.
Downloaded ``SharedArray`` backing data is similarly owned by the bundle.
Declared external paths preserve their original values and are never copied or reinterpreted from spelling alone.

Attached execution
------------------

Direct, Wetlands, and attached Parsl share an engine-neutral export path:

.. code-block:: python

   context = WorkflowExecutionContext()
   result = workflow.compute(target, run_context=context)
   exported = context.export_result(
       result,
       destination=downloads / context.run_id,
   )

The context retains the run ID, target binding, and :class:`~bioimageflow.ExecutionProviderOutcome` values used to classify assets.
Attached export creates no run and allocates no worker or scheduler resource.

The snapshot is taken when ``export_result()`` is called.
It is process-local and must run before caller mutation of the DataFrame or removal of transient or return-owned files.
After successful installation, repeating export to the same destination uses the retained expected bundle digest and does not require transient sources again.

Errors and cleanup
------------------

All export failures derive from :class:`~bioimageflow.WorkflowResultExportError`.
Bundle or snapshot verification raises :class:`~bioimageflow.WorkflowResultIntegrityError`, and an unsafe, incomplete, or foreign destination raises :class:`~bioimageflow.WorkflowResultDestinationError`.
Transport failures alone use :class:`~bioimageflow.SSHTransportError`.
A failed or interrupted installation removes its private partial sibling and never exposes an incomplete destination.
:class:`~bioimageflow.WorkflowRunResultUnavailableError` indicates that a successful retained return can no longer be reconstructed because required immutable data was pruned or corrupted.
