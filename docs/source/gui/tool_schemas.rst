Tool Schemas
============

Hosts that render inline form widgets — pin lists, parameter editors,
output channel previews — need a JSON-safe description of every tool's
inputs and outputs. The :mod:`bioimageflow.validation` module provides
two serializers and two helpers tuned for that use case.

Input schema
------------

:func:`~bioimageflow.validation.serialize_input_schema` walks
``tool_class.Inputs`` (with MRO) and returns a JSON-safe dict, one
entry per field:

.. code-block:: python

   from bioimageflow.validation import serialize_input_schema

   schema = serialize_input_schema(MyTool)
   # {
   #     "image": {
   #         "type": "ImageFile", "required": True, "nullable": False,
   #         "connectable": "by_default", "default": None,
   #         "display_name": "Input image", "description": "...",
   #         "group": None, "min": None, "max": None, "step": None,
   #         "choices": None,
   #         "image_spec": {"semantics": ["intensity"], "layouts": [], ...},
   #     },
   #     "sigma": {
   #         "type": "float", "required": False, "nullable": False,
   #         "connectable": "not_by_default", "default": 1.0,
   #         "min": 0.1, "max": 50.0, "step": 0.1, ...
   #     },
   # }

Per-field keys:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Key
     - Description
   * - ``type``
     - Display-name string for the annotation (``"float"``, ``"int"``,
       ``"Path"``, ``"ImageFile"``, ...).
   * - ``required``
     - ``True`` when the field has no class-level default. Orthogonal
       to ``Optional[X]``.
   * - ``nullable``
     - ``True`` when the annotation includes ``None`` / ``Optional``.
   * - ``connectable``
     - ``"never"`` / ``"not_by_default"`` / ``"by_default"`` — see
       below.
   * - ``default``
     - JSON-safe representation of the class-level default; ``None``
       for required fields.
   * - ``display_name``, ``description``, ``group``
     - Free-form strings from :class:`~bioimageflow_core.GUIMeta`;
       ``None`` when absent.
   * - ``min``, ``max``, ``step``
     - Numeric widget bounds from ``GUIMeta``; ``None`` when absent.
   * - ``choices``
     - List of strings for ``Literal[...]`` / :class:`enum.Enum`
       fields; ``None`` otherwise.
   * - ``image_spec``
     - Dict produced by :func:`serialize_image_spec` (see below);
       ``None`` when the field is not a typed image.

Returns ``{}`` when the tool has no ``Inputs`` class.

The function does not instantiate the tool — it walks annotations and
class-level defaults. ``SchemaSerializationError`` is reserved for the
rare introspection failure (typically when ``Inputs`` cannot be
inspected at all).

Connectable
-----------

The ``connectable`` field surfaces the
:class:`~bioimageflow_core.Connectable` enum, serialized as a string:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - String value
     - Enum value
     - Suggested UI mapping
   * - ``"never"``
     - ``Connectable.NEVER``
     - No pin; pure config field.
   * - ``"not_by_default"``
     - ``Connectable.NOT_BY_DEFAULT``
     - Pin hidden; reveal via "expose as pin" toggle.
   * - ``"by_default"``
     - ``Connectable.BY_DEFAULT``
     - Pin visible; data input.

For ``Outputs``, ``connectable`` is ignored by the runtime because outputs
always expose a pin. When an output carries ``GUIMeta``, the serializer still
emits the string value so frontends can preserve the full metadata object.

Output schema
-------------

:func:`~bioimageflow.validation.serialize_output_schema` returns a
similar dict, with simpler per-field shape. Outputs include GUI metadata
keys only when the output annotation carries
:class:`~bioimageflow_core.GUIMeta`.

.. code-block:: python

   {
       "mask": {
           "type": "ImageFile",
           "default": "{input_image.stem}_mask{ext}",
           "image_spec": {...},
           "template": "{input_image.stem}_mask{ext}",
           "connectable": "not_by_default",
           "display_name": "Segmentation mask",
           "description": "Label image.",
           "group": None,
           "min": None,
           "max": None,
           "step": None,
       }
   }

Two special cases:

- ``{}`` when the tool has no ``Outputs`` class.
- ``{"_passthrough": True}`` when ``Outputs`` is — or subclasses —
  :class:`~bioimageflow.Passthrough`. GUIs should render this as
  "inherits upstream columns".

For tools with **dynamic** output columns (``Generate``, the merge
tools), the static schema only covers fields declared on ``Outputs``.
The actually-resolved schema for a configured node is available via
:func:`~bioimageflow.validation.serialize_resolved_outputs(node)` —
useful for rendering per-column output pins.

serialize_image_spec
--------------------

:func:`~bioimageflow.validation.serialize_image_spec` converts an
:class:`~bioimageflow_core.ImageSpec` (or ``None``) into a JSON-safe
dict of sorted-string lists:

.. code-block:: python

   {
     "semantics": ["intensity"],
     "layouts":   ["YX", "CYX"],
     "dtypes":    [],
     "formats":   ["memory"],
   }

Enum members are written as their string values
(``"intensity"`` not ``Semantic.INTENSITY``). Empty sets are wildcards
— consistent with the in-memory semantics.

In-memory introspection
-----------------------

When the host is in the same process as the tool class and just wants
Python objects (raw ``type`` annotations, raw
:class:`~bioimageflow_core.Connectable` values),
:func:`~bioimageflow.validation.get_inputs_schema` returns the
non-serialized form:

.. code-block:: python

   from bioimageflow import get_inputs_schema

   schema = get_inputs_schema(my_tool_instance)
   schema["sigma"]["type"]         # <class 'float'>
   schema["sigma"]["connectable"]  # Connectable.NOT_BY_DEFAULT

Use ``serialize_input_schema`` when shipping the schema over the wire
(GUI server → frontend, plugin host → editor); use
``get_inputs_schema`` for in-process introspection that needs the live
type objects.

SchemaSerializationError
------------------------

:class:`~bioimageflow.validation.SchemaSerializationError` is raised
when the serializers cannot produce a wire-format schema — typically
because the tool class could not be instantiated for introspection.
:class:`~bioimageflow.ToolRegistry` swallows the error and falls back
to ``{}`` so a single broken class does not block the rest of a tool
package; direct callers should catch it and surface the failure.
