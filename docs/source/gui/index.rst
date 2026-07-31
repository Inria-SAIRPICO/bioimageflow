GUI / Frontend Integrators
==========================

This tree targets developers who embed BioImageFlow in a long-lived process —
graphical editors, web servers, plugin hosts. It documents the wire format,
the incremental editing model, the validation surface, and the tool-schema
serializers needed to drive a graph from outside the workflow-author API.

Workflow scripts and tool authors should read the
:doc:`/concepts/index` and :doc:`/tutorials/index` trees instead.

.. toctree::
   :maxdepth: 1

   overview
   wire_format
   from_dict
   workflow_session
   tool_registry
   live_validation
   planning_and_cache
   disabled_nodes
   progress_and_cancel
   submitted_parsl
   tool_schemas
