Reusable and Nested Workflows
=============================

A reusable workflow is an ordinary :class:`~bioimageflow.Workflow` with a public interface.
Put the definition in a ``build_workflow(storage_path=...)`` factory so each materialization is fresh and receives its runtime storage explicitly.

.. code-block:: python

   from pathlib import Path

   from bioimageflow import Workflow

   WORKFLOW_DIRECTORY = Path(__file__).resolve().parent

   def build_workflow(
       *,
       storage_path: str | Path = WORKFLOW_DIRECTORY / "results",
   ) -> Workflow:
       workflow = Workflow(
           name="segment",
           display_name="Segment",
           storage_path=storage_path,
       )
       with workflow:
           image = workflow.input("image", ImagePath, id="input-image")
           masks = Segment()(input_image=image, name="segment")
           workflow.output("mask", masks["mask"], id="output-mask")
       return workflow

Calling a workflow inside a distinct active parent captures an independent :class:`~bioimageflow.WorkflowNode`:

.. code-block:: python

   child = build_workflow()
   parent = Workflow(name="parent", storage_path="./parent-results")
   with parent:
       source = Files()(path="images", name="files")
       nested = child(image=source["path"], name="segment-images")
       parent.output("mask", nested["mask"], id="output-mask")

Root callers pass interface values with ``compute(inputs={...})``.
Nested tools execute under scoped paths such as ``segment-images/segment`` and keep their own cache entries.
The workflow boundary succeeds only after every enabled internal terminal, including detached branches, succeeds or hits cache.

Use :meth:`~bioimageflow.Workflow.from_python` only for trusted Python definitions.
Portable exports contain the materialized recursive graph and never need to run the factory again.
