Sub-Workflows
=============

A :class:`~bioimageflow.SubWorkflow` is a reusable pipeline-as-a-node:
a small DAG with declared inputs and outputs that you can drop into a
larger workflow the same way you drop in a tool. Internal nodes are
cached individually, so a sub-workflow benefits from incremental
re-execution just like the top-level graph.

Sub-workflows are the right tool when:

- A short sequence of tools (``preprocess → segment → measure``) is reused
  across multiple workflows.
- A team wants to ship a pipeline as a single composable unit, with its
  own ``Inputs`` / ``Outputs`` contract, rather than expose every internal
  step.

For one-off compositions inside a script, just connect the tools
directly — no sub-workflow needed.

Defining a sub-workflow
-----------------------

A sub-workflow subclass declares ``Inputs``, ``Outputs``, and a
``build(self, inputs)`` method. ``build`` constructs the internal DAG
using ordinary tool calls and returns a dict mapping every output name
to a :class:`~bioimageflow.ColumnRef` from one of the internal nodes:

.. code-block:: python

   from bioimageflow import SubWorkflow
   from bioimageflow_core import IOModel, ImagePath
   from bioimageflow_common_tools import ConvertImage
   from my_tools import Threshold, Measure

   class SegmentAndMeasure(SubWorkflow):
       display_name = "Segment and Measure"

       class Inputs(IOModel):
           image: ImagePath()
           cutoff: float = 128.0

       class Outputs(IOModel):
           mask: ImagePath()
           area: float

       def build(self, inputs):
           prep = ConvertImage()(image=inputs.image)
           mask = Threshold()(image=prep["image"], cutoff=inputs.cutoff)
           stats = Measure()(mask=mask["mask"])
           return {"mask": mask["mask"], "area": stats["area"]}

The ``inputs`` argument is a proxy — its attributes are
:class:`~bioimageflow.ColumnRef` objects (for column-bound inputs) or the
literal value (for constants), so internal nodes can reference upstream
inputs by attribute access. The dict returned from ``build`` must contain
every name declared in ``Outputs``; missing keys raise ``ValueError``,
extra keys produce a warning and are ignored.

Using a sub-workflow
--------------------

Calling a sub-workflow instance is identical to calling a tool — the
parent workflow sees a single :class:`~bioimageflow.SubWorkflowNode`:

.. code-block:: python

   from bioimageflow import Workflow
   from bioimageflow_common_tools import Files

   pipeline = SegmentAndMeasure()
   files = Files()

   with Workflow() as wf:
       images = files(path="/data", pattern="*.tif")
       result = pipeline(image=images["path"], cutoff=100.0)
       df = wf.compute(result)

   df["mask"]    # output column declared by Outputs.mask
   df["area"]    # output column declared by Outputs.area

Bindings follow the same rules as ProcessingTool calls
(see :doc:`/concepts/graph`):
:class:`~bioimageflow.ColumnRef`, node-shorthand
(``pipeline(image=images)`` resolves to ``images["image"]``), or
constants. Construction-time validation runs the same way too — passing
an unknown input raises :class:`~bioimageflow.BindingError`.

Flattening and name scoping
---------------------------

Internally, a sub-workflow is **flattened** into the parent graph at
execution time. Every internal node is given a scoped name of the form
``<sub_workflow_name>/<internal_name>``:

.. code-block:: text

   Top-level graph:
       files
       segment_and_measure
   After flattening:
       files
       segment_and_measure/convert_image
       segment_and_measure/threshold
       segment_and_measure/measure

The scoped form is what shows up in cache directories, ``plan()``
results, ``compute_steps()`` iteration, and ``ProgressEvent.node_name``
during execution.

Per-internal-node caching
-------------------------

Each internal node caches independently — there is no aggregate
"sub-workflow cache". If you re-run the parent workflow after editing
just ``Threshold``'s ``cutoff``, the engine recomputes
``segment_and_measure/threshold`` and ``segment_and_measure/measure`` and
reuses the cached output of ``segment_and_measure/convert_image``.

In :meth:`~bioimageflow.Workflow.plan`, a sub-workflow node aggregates
its internals: it reports ``CACHED`` only when *every* internal entry is
cached, and ``UNEXECUTED`` otherwise. See :doc:`/concepts/caching` for
the four ``NodePlanStatus`` values.

Nesting
-------

Sub-workflows can call other sub-workflows from inside ``build``:

.. code-block:: python

   class FullPipeline(SubWorkflow):
       display_name = "Full Pipeline"

       class Inputs(IOModel):
           image: ImagePath()
           cutoff: float = 128.0

       class Outputs(IOModel):
           mask: ImagePath()
           area: float

       def build(self, inputs):
           prep = ConvertImage()(image=inputs.image)
           inner = SegmentAndMeasure()(
               image=prep["image"], cutoff=inputs.cutoff,
           )
           return {"mask": inner["mask"], "area": inner["area"]}

Scoping composes:
``full_pipeline/segment_and_measure/threshold`` is the threshold node
two levels in. The depth is unbounded.

Debugging with compute_steps
----------------------------

To see the full flattened execution order — including every internal
node — iterate :meth:`~bioimageflow.Workflow.compute_steps` and print
``step.node_name``:

.. code-block:: python

   with Workflow() as wf:
       files = Files()
       images = files(path="/data", pattern="*.tif")
       result = pipeline(image=images["path"])

       for step in wf.compute_steps([result]):
           print(step.node_name, step.skipped)
           step.execute()
   # files                              False
   # segment_and_measure/convert_image  False
   # segment_and_measure/threshold      False
   # segment_and_measure/measure        False

The full ``compute_steps`` surface — disabled-node propagation,
``DisabledNodeError``, manual stepping for GUIs — lives in the
GUI tree.

Wire-format round-trip
----------------------

When a sub-workflow is serialized via
:meth:`~bioimageflow.Workflow.to_dict` it is preserved as a single node
with extra keys (``type``, ``sub_workflow_module``, ``internal_nodes``,
``internal_edges``); :meth:`~bioimageflow.Workflow.from_dict` reconstructs
the full internal DAG. The wire-format details and the
``WorkflowSession`` editing model live under the GUI / Platform
Integrators tree.

Config-driven sub-workflows
---------------------------

GUIs that need to define sub-workflows at runtime (without a Python
class on disk) can use :meth:`SubWorkflow.from_config`, which
materialises a ``SubWorkflow`` from a JSON dict — see specs.md §14.11.
Workflow-author scripts should write a normal subclass instead; the
config form is targeted at platform code.
