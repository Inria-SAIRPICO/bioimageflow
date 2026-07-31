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
