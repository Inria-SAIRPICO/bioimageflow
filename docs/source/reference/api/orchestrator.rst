bioimageflow
============

The orchestrator package. Main process only. Depends on pandas and pydantic.

Workflow
--------

.. automodule:: bioimageflow.workflow
   :members:
   :undoc-members:
   :show-inheritance:

Node
----

.. automodule:: bioimageflow.node
   :members:
   :undoc-members:
   :show-inheritance:

Engine
------

.. automodule:: bioimageflow.engine
   :members:
   :undoc-members:
   :show-inheritance:

DataFrameTool
-------------

.. automodule:: bioimageflow.dataframe_tool
   :members:
   :undoc-members:
   :show-inheritance:

Cache
-----

.. automodule:: bioimageflow.cache
   :members:
   :undoc-members:
   :show-inheritance:

Storage
-------

Low-level cache storage primitives for the current ``cache/v1`` storage layout.

.. automodule:: bioimageflow.storage
   :members:
   :undoc-members:
   :show-inheritance:

Template
--------

.. automodule:: bioimageflow.template
   :members:
   :undoc-members:
   :show-inheritance:

Tool registry
-------------

.. automodule:: bioimageflow.registry
   :members:
   :undoc-members:
   :show-inheritance:

Workflow session
----------------

.. automodule:: bioimageflow.session
   :members:
   :undoc-members:
   :show-inheritance:

Workflow nodes
--------------

.. automodule:: bioimageflow.workflow_node
   :members:
   :undoc-members:
   :show-inheritance:

Wetlands configuration
----------------------

.. automodule:: bioimageflow.env_manager
   :members:
   :undoc-members:
   :show-inheritance:

Portable resources and distributed planning
-------------------------------------------

.. automodule:: bioimageflow.resources
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: bioimageflow.planning
   :members:
   :undoc-members:
   :show-inheritance:

Platform integration contracts
------------------------------

.. automodule:: bioimageflow.integration
   :members:
   :undoc-members:
   :show-inheritance:

Submitted execution
-------------------

.. automodule:: bioimageflow.launcher.prepared
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: bioimageflow.launcher.profile_validation
   :members:
   :undoc-members:
   :show-inheritance:

Validation
----------

The validation surface (errors, schema serializers, single-field
helpers) has many internal helpers that ``automodule`` would surface
verbatim. See :doc:`validation` for the curated reference, and
:doc:`/reference/errors` for the error catalogue.

Exact output planning
---------------------

The public planner checks declared file owners before affected scientific work.
``OutputPlanningError.report`` contains a detached explanation; optional
``evidence.check_templates(overrides)`` checks all retained lightweight inputs
without editing the workflow or starting workers.
Evidence is bounded and may be unavailable for later correction, while the
explanation remains readable.
``inspect_workflow_output_files(workflow)`` returns only conflicts provable from
the current constant-input graph; unknown upstream values remain silent.
This inspection does not hydrate scientific results or execute tool callbacks.

.. automodule:: bioimageflow.output_planning
   :members: OutputPlanningError, OutputPathCollisionError, OutputGroupDestinationMismatchError, OutputPlanningEvidence, OutputFilePlan, output_file_declarations, inspect_output_files, inspect_workflow_output_files
