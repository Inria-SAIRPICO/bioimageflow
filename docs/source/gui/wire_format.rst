Wire Format
===========

``Workflow.to_dict()`` produces the strict recursive schema-version-1 graph used by editors and hosts.
The complete field grammar and golden fixtures are documented in :doc:`/reference/unified_workflow_contract`.

Top-level shape
---------------

.. code-block:: json

   {
     "schema_version": 1,
     "name": "parent",
     "display_name": "Parent",
     "interface": {"inputs": [], "outputs": []},
     "nodes": [],
     "edges": [],
     "config": {}
   }

Tool and workflow nodes
-----------------------

Tool nodes use ``"type": "tool"`` and carry their module, class, package identity, constants, templates, and enabled state.
Nested definitions use ``"type": "workflow"`` and inline the same graph shape recursively.
Their constant bindings are keyed by stable child-input IDs.

Edges
-----

Column and complete-DataFrame connections are distinct variants.
Every edge has a stable opaque ``id`` used by editor selection and ``ValidationError.edge_id``.
Column endpoints use a tool field name or stable workflow port ID.
DataFrame endpoints use a positional tool index or stable workflow DataFrame-input ID.

Portable archives
-----------------

``Workflow.export()`` collects recursively used custom sources once:

.. code-block:: json

   {
     "archive_version": 1,
     "workflow": {"schema_version": 1},
     "custom_sources": []
   }

Tool nodes refer to source records with ``source_module``.
Source IDs plus verified content prevent equal class names in different modules from shadowing one another.

Strict parsing
--------------

Unknown fields, variants, duplicate IDs, malformed endpoints, and unversioned graphs are rejected.
``WorkflowSession`` retains this graph as its source of truth and materializes it through ``Workflow.from_dict``.
