Reusable and Nested Workflows
=============================

A reusable workflow is an ordinary :class:`~bioimageflow.Workflow` with a public interface.
Put the definition in a no-argument ``build_workflow`` factory so each materialization is fresh.

.. code-block:: python

   from bioimageflow import Workflow

   def build_workflow() -> Workflow:
       workflow = Workflow(name="segment", display_name="Segment")
       with workflow:
           image = workflow.input("image", ImagePath, id="input-image")
           masks = Segment()(input_image=image, name="segment")
           workflow.output("mask", masks["mask"], id="output-mask")
       return workflow

Calling a workflow inside a distinct active parent captures an independent :class:`~bioimageflow.WorkflowNode`:

.. code-block:: python

   child = build_workflow()
   parent = Workflow(name="parent")
   with parent:
       source = Files()(path="images", name="files")
       nested = child(image=source["path"], name="segment-images")
       parent.output("mask", nested["mask"], id="output-mask")

Root callers pass interface values with ``compute(inputs={...})``.
Nested tools execute under scoped paths such as ``segment-images/segment`` and keep their own cache entries.
The workflow boundary succeeds only after every enabled internal terminal, including detached branches, succeeds or hits cache.

Use :meth:`~bioimageflow.Workflow.from_python` only for trusted Python definitions.
Portable exports contain the materialized recursive graph and never need to run the factory again.
