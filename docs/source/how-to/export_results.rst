Export Results
==============

Destination-based export creates a run-specific immutable bundle that can be downloaded, shared, and reopened independently of mutable workflow views.

Export a submitted result
-------------------------

Local and remote submitted runs use the same call:

.. code-block:: python

   destination = downloads / run.id
   result = run.export_result(destination)

The destination's parent directory must already exist.
BioImageFlow builds or downloads a private sibling bundle, verifies its manifest and every file digest, and installs the destination atomically.
Calling the method again with the same verified destination is safe.
A conflicting, incomplete, or corrupt destination is rejected.

Record-owned and return-owned assets are copied beneath the bundle and returned DataFrame paths point there.
Paths declared external remain external values.

A bundle is self-contained for owned assets, not for those external dependencies.
Shared-array rehydration retains an accessible controller owner independent of the temporary execution object.
Accepted S target: callers can explicitly release the exact returned allocation group, with truthful pending views/grants and no deletion of durable bundle assets; API conformance remains a T/C obligation.
For a local :class:`~bioimageflow.WorkflowRun`, ``run.load_result()`` reads the successful return directly from workflow storage without creating a portable bundle.

Export an attached result
-------------------------

Keep a :class:`~bioimageflow.WorkflowExecutionContext` when calling ``compute()``:

.. code-block:: python

   from bioimageflow import WorkflowExecutionContext

   context = WorkflowExecutionContext()
   result = workflow.compute(target, run_context=context)
   portable = context.export_result(
       result,
       destination=downloads / context.run_id,
   )

This works with Direct, Wetlands, and attached Parsl execution because export consumes engine-neutral :class:`~bioimageflow.ExecutionProviderOutcome` values.
Only a successfully completed context can export a result.

Attached export is a process-local snapshot taken when ``export_result()`` is called.
Call it before mutating the returned DataFrame or deleting transient or return-owned files.
Once installed, repeating export to that same verified destination does not need the transient source assets again.
