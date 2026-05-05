Cancellation
============

Long-running workflows — interactive GUIs, server-driven pipelines, or
just any compute that may need to stop mid-flight — should be cancellable.
BioImageFlow exposes a single :meth:`~bioimageflow.Workflow.cancel` method
that flips a thread-safe flag the engine checks before each row.

Requesting cancellation
-----------------------

:meth:`Workflow.cancel` sets a flag on the workflow object. The flag is
thread-safe (backed by a ``threading.Event``), so a GUI thread can call
it while ``compute()`` is running on a background thread:

.. code-block:: python

   wf.cancel()

The engine checks ``Workflow.cancel_requested`` before each node and
between rows. When the flag is set, the engine raises
:class:`~bioimageflow.engine.WorkflowCancelledError` from the in-flight
``compute()`` call.

In-flight rows
--------------

When a row is mid-execution at the moment the flag flips, the engine
calls ``task.cancel()`` on the corresponding Wetlands handle. The cancel
is **cooperative**: Wetlands tells the worker process that cancellation
was requested, but cannot interrupt arbitrary native code. Tools that
expect to run for a long time should poll the cancellation hint
themselves.

Tool-side cooperative cancellation
----------------------------------

A ``ProcessingTool`` opts in by adding a ``task`` keyword argument to
``process_row`` (or ``process_batch``). The runtime injects the Wetlands
``RemoteTaskHandle``; the tool checks ``task.cancel_requested`` at safe
points in its loop:

.. code-block:: python

   class SlowSegment(ProcessingTool):
       display_name = "Slow Segment"
       environment = EnvironmentSpec(name="torch", dependencies={...})

       class Inputs:
           image: Annotated[Path, ImageSpec()]

       class Outputs:
           mask: Annotated[Path, ImageSpec()] = Template("{image.stem}_mask.tif")

       def process_row(self, arguments, *, task=None):
           img = self._load(arguments.image)
           for i, tile in enumerate(self._tiles(img)):
               if task is not None and task.cancel_requested:
                   raise RuntimeError("cancelled")
               self._process_tile(tile)
           ...

The runtime detects the keyword via signature inspection — tools without
a ``task`` parameter run unchanged.

DataFrameTools are not cooperatively cancellable: ``transform`` and
``merge_dataframes`` run on the main thread, so a long pandas operation
will complete before the cancel takes effect.

Surfacing the cancellation
--------------------------

Once the engine raises :class:`~bioimageflow.engine.WorkflowCancelledError`,
``compute()`` propagates it to the caller. Catch it where you call
``compute()``:

.. code-block:: python

   from bioimageflow.engine import WorkflowCancelledError

   try:
       result = wf.compute(target)
   except WorkflowCancelledError:
       # GUI: render the partial state, leave already-cached nodes alone
       ...

Worked example: cancellable background compute
----------------------------------------------

A typical GUI pattern: spawn ``compute()`` on a thread, expose a "Cancel"
button that calls ``wf.cancel()``:

.. code-block:: python

   import threading
   from bioimageflow.engine import WorkflowCancelledError

   def run():
       try:
           wf.compute(target)
       except WorkflowCancelledError:
           print("cancelled")

   thread = threading.Thread(target=run, daemon=True)
   thread.start()

   # Later — perhaps from a GUI button handler:
   wf.cancel()
   thread.join()
