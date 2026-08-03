Resources and Concurrency
=========================

Resource requirements are portable descriptions used by planning and dispatch.
A tool declares a safe minimum, while one workflow node may request more resources or a stricter concurrency limit.

Tool declarations
-----------------

A ProcessingTool declares :class:`~bioimageflow_core.ResourceSpec` on its class:

.. code-block:: python

   from bioimageflow_core import ProcessingTool, ResourceSpec

   class Segment(ProcessingTool):
       resources = ResourceSpec(
           cpu=4,
           gpu=1,
           memory="16GB",
           gpu_memory="8GB",
           max_concurrent=4,
       )

``cpu`` is a positive integer and ``gpu`` is a non-negative integer.
``memory`` and ``gpu_memory`` accept positive integral capacities with ``B``, ``KB``, ``MB``, ``GB``, ``TB``, or their ``KiB`` variants.
``max_concurrent=0`` means that the declaration imposes no finite node limit.

Node-specific overrides
-----------------------

:class:`~bioimageflow.NodeResourceOverrides` belongs to a ProcessingTool node instance, not to its tool class:

.. code-block:: python

   from bioimageflow import NodeResourceOverrides

   large_images.set_resource_overrides(
       NodeResourceOverrides(
           cpu=8,
           memory="32GB",
           max_concurrent=2,
       )
   )

Two nodes created from the same tool instance or class may therefore request different resources.
DataFrameTool nodes reject worker resource overrides because they run in the orchestrator.

Merge and validation rules
--------------------------

An omitted override field inherits the tool declaration.
The effective value obeys these rules:

- ``cpu``, ``gpu``, ``memory``, and ``gpu_memory`` may equal or exceed the tool declaration, but cannot lower its floor;
- a finite ``max_concurrent`` may only stay equal or become stricter;
- when the tool declaration is unlimited, an override may introduce a finite limit;
- ``max_concurrent=0`` cannot remove a finite tool limit.

Invalid values fail when the override is attached and again during graph validation or deserialization.
Use ``node.effective_resources`` to inspect the normalized merge for one node.

Serialization and cache identity
--------------------------------

Node overrides round-trip in public workflow graph serialization at every nested workflow depth.
They affect placement and submission pressure, but do not change result keys or cache record identities.
Changing a resource request can move or resize execution without claiming that the scientific result changed.

Engine guarantees
-----------------

.. list-table::
   :header-rows: 1
   :widths: 24 22 54

   * - Mode
     - Enforced fields
     - Meaning
   * - Local
     - ``max_concurrent``
     - BioImageFlow bounds active row tasks; CPU, GPU, and memory fields remain visible planning requirements
   * - Direct
     - Sequential dispatch
     - No placement or allocation is performed
   * - Parsl
     - All fields
     - CPU, GPU, and memory requirements must fit an attested worker slot; ``max_concurrent`` bounds unfinished node tasks

BioImageFlow does not mutate individual local worker environments to assign GPUs.
GPU visibility for local workers must be established outside the library.
Parsl profiles instead advertise GPU-capable worker slots and route tasks to compatible executors.

Distributed planning
--------------------

:func:`~bioimageflow.plan_distributed_execution` returns each scoped ProcessingTool node's effective requirement, compatible executors, selected route when unambiguous, and structured incompatibility reasons.
It shares requirement, compatibility, and routing logic with runtime dispatch.
It does not import Parsl, start a DataFlowKernel, allocate workers, submit a scheduler job, or create a workflow run.

See :doc:`routing` for executor capacity, environment compatibility, tool origins, and explicit routes.
