Wire Format
===========

The accepted current graph and archive contracts are specified in :doc:`/reference/unified_workflow_contract`.
Implementation and fixture conformance are assessed in T/C; this clarification does not certify source behavior.
Graph/archive versions are independent of viewer-manifest and Core processing DTO schema versions.

Editable graph
--------------

``Workflow.to_dict()`` describes one recursive graph, with runtime storage supplied separately:

.. code-block:: json

   {
     "schema_version": 2,
     "name": "parent",
     "display_name": "Parent",
     "interface": {"inputs": [], "outputs": []},
     "nodes": [],
     "edges": [],
     "config": {}
   }

Tool nodes carry exact package/source identity, constant envelopes, templates, enabled state and optional viewer additions.
Workflow nodes inline the same complete graph grammar; constant bindings use stable child-input IDs.
Column and complete-DataFrame edges are distinct variants with stable IDs and explicit endpoints.
Presentation renames do not change port or edge connectivity.

Portable archive
----------------

``Workflow.to_archive_dict()`` and ZIP export describe the artifact envelope; ZIP stores it as ``workflow.json``:

.. code-block:: json

   {
     "archive_version": 2,
     "workflow": {
       "schema_version": 2,
       "name": "parent",
       "display_name": "Parent",
       "interface": {"inputs": [], "outputs": []},
       "nodes": [],
       "edges": [],
       "config": {}
     },
     "custom_sources": [],
     "viewing_requirements": {
       "schema": "bioimageflow.viewing_requirements.v1",
       "complete": true,
       "outputs": {}
     }
   }

Used custom sources are collected once across the recursive graph.
Source IDs plus verified captured bytes identify executable content; class names or catalog aliases do not.
Viewer requirements are portable diagnostic metadata and do not install packages or affect computation identity.

Admission and editing
---------------------

Accepted target intent refuses unknown fields/variants, unsupported versions, missing required fields, duplicate IDs and malformed endpoints without guessing or silent conversion.
Inner source-table and outer ZIP/destination admission are separate; trusted embedded Python is not sandboxed.
``WorkflowSession`` owns editable graph state and materializes a projection through ``Workflow.from_dict``.
Diagnostic editing can retain incomplete current values with scoped errors; legacy adapters and a second graph authority are not requirements.
