Wire Format
===========

The wire format is the JSON-shaped dict produced by
:meth:`~bioimageflow.Workflow.to_dict` and consumed by
:meth:`~bioimageflow.Workflow.from_dict`. It is the canonical shape for
hosts that store, transport, edit, or version-control workflows.

Top-level shape
---------------

.. code-block:: text

   {
     "nodes":  [ ... ],
     "edges":  [ ... ],
     "config": { ... }
   }

- ``nodes`` — list of node entries (see below).
- ``edges`` — list of column-binding edges between nodes.
- ``config`` — workflow-level settings: ``storage_path``, ``engine``,
  ``max_executions``, ``max_age``.

The format is JSON-serializable; ``Workflow.export(path)`` writes it via
``json.dumps(..., indent=2, default=str)``.

Worked example
--------------

A minimal two-node graph (a source ``Files`` feeding a ``Threshold``
processing tool) round-trips as:

.. code-block:: json

   {
     "nodes": [
       {
         "name": "files",
         "tool_module": "bioimageflow_common_tools.files",
         "tool_class": "Files",
         "tool_package": "bioimageflow_common_tools",
         "tool_package_version": "0.1.0",
         "constants": {
           "path":    {"__type__": "str",  "value": "/data"},
           "pattern": {"__type__": "str",  "value": "*.tif"}
         },
         "args": []
       },
       {
         "name": "threshold",
         "tool_module": "my_tools.threshold",
         "tool_class": "Threshold",
         "tool_package": "my_tools",
         "tool_package_version": "1.0.0",
         "constants": {
           "cutoff": {"__type__": "float", "value": 100.0}
         },
         "args": []
       }
     ],
     "edges": [
       {"from": "files", "to": "threshold", "column": "path", "field": "image", "id": "e1"}
     ],
     "config": {
       "storage_path": "./bif_data",
       "engine": "sequential",
       "max_executions": 0,
       "max_age": null
     }
   }

Each ``args`` entry is a node name (positional upstream); column
bindings are written as ``edges`` with ``column`` / ``field``.

Constant envelope
-----------------

Constants are wrapped in a small envelope:

.. code-block:: text

   {"__type__": "<type>", "value": <encoded_value>}

The ``__type__`` field is one of ``"bool"``, ``"int"``, ``"float"``,
``"str"``, ``"list"``, ``"tuple"``. Lists and tuples encode each element
with its own envelope. Anything outside this set is *lossy* — it
serializes via ``str(value)`` with ``__type__: "str"``, so unknown types
become strings on the way out and stay strings on the way back in.

Use :func:`~bioimageflow.validation.serialize_constant` and
:func:`~bioimageflow.validation.deserialize_constant` directly if you
need to encode constants outside ``Workflow.to_dict()``:

.. code-block:: python

   from bioimageflow.validation import serialize_constant, deserialize_constant

   serialize_constant(3.14)              # {"__type__": "float", "value": 3.14}
   deserialize_constant({"__type__": "int", "value": 7})  # 7

Edge id
-------

Each ``edge`` may carry an opaque ``id``:

.. code-block:: json

   {"from": "files", "to": "threshold", "column": "path", "field": "image", "id": "e_2"}

The library treats ``id`` as opaque — it round-trips it through
``to_dict`` / ``from_dict`` and copies it onto every
:class:`~bioimageflow.validation.ValidationError` raised against that
edge (via ``ValidationError.edge_id``). A host that needs to highlight
the offending edge in a visual editor sets ``id`` when adding the edge
and then matches ``edge_id`` on validation errors.

The id also disambiguates **positional** edges, where multiple edges may
share the same ``(from, to, column="__positional__", field="__positional__")``
triple by construction.

Disabled nodes
--------------

A node that is currently disabled carries ``"enabled": false``:

.. code-block:: text

   {"name": "filter", "tool_module": "...", "tool_class": "Filter", "enabled": false, ...}

Enabled is the default; the key is **omitted** for enabled nodes (the
"clean form" rule). :class:`~bioimageflow.WorkflowSession` enforces this
on every ``set_enabled`` call so that toggling on never leaves a stray
``"enabled": true``.

Sub-workflow nodes
------------------

Sub-workflow nodes carry extra keys:

.. code-block:: text

   {
     "name": "segment_and_measure",
     "type": "sub_workflow",
     "sub_workflow_module": "my_tools.pipeline",
     "sub_workflow_class": "SegmentAndMeasure",
     "sub_workflow_package": "my_tools",
     "sub_workflow_package_version": "1.0.0",
     "constants": { ... }
   }

Config-driven sub-workflows (those built via
:meth:`SubWorkflow.from_config`) carry ``"sub_workflow_type": "config"``
and a ``config`` dict instead of module / class names — see specs.md
§14.11 for the config schema.

Round-trip identity
-------------------

For a workflow ``wf`` reconstructed from a dict ``data``,
``Workflow.from_dict(data).to_dict()`` produces a dict that is **equal**
to the input modulo the clean-form rule for ``enabled``. The same is
true for :class:`~bioimageflow.WorkflowSession`:
``WorkflowSession(data).to_dict()`` returns a deep copy of the wire
format byte-for-byte (with ``"enabled": false`` stripped on
re-enable).

This is the contract editors rely on — the wire format is the source of
truth, not the materialized ``Workflow``.
