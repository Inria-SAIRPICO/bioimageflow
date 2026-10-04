Tool Schemas
============

Hosts that render inline form widgets — pin lists, parameter editors,
output channel previews — need a JSON-safe description of every tool's
inputs and outputs. The :mod:`bioimageflow.validation` module provides
class-based serializers and configured-node resolution for that use case.
The complete field catalogue and type-display rules are authoritative in :doc:`/specs` §2.4; this guide explains their use.

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
   #         "path_picker": None, "choices": None,
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
   * - ``type_spec``
     - Current finite semantic annotation descriptor; unlike ``type``, it preserves primitive/Path/reference/array, union, collection and choice structure for loaded schemas.
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
   * - ``path_picker``
     - ``"file"``, ``"folder"``, ``"both"`` or ``None``; a GUI-only path-selection hint.
   * - ``choices``
     - JSON-safe declared choices for ``Literal[...]`` / :class:`enum.Enum` fields; ``None`` otherwise.
   * - ``image_spec``
     - Dict produced by :func:`serialize_image_spec` (see below);
       ``None`` when the field is not a typed image.

Returns ``{}`` when the tool has no ``Inputs`` class.

The function does not instantiate the tool — it walks annotations and
class-level defaults. ``SchemaSerializationError`` is reserved for the
explicit semantic resolution error; unsupported declarations/default projections must not become empty schema success.

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

DataFrameTool parameter fields always serialize ``connectable="never"``; positional upstream DataFrames are separate from these fields.
Required, nullable and default are orthogonal: a nullable field without a default still requires a supplied value.
Schema type strings are display metadata, not replacement Python annotations.
``type_spec`` is admitted without evaluating Python expressions, while image, GUI and viewer metadata remain explicit fields.
Output entries also retain requiredness and nullability; returned schema/default dictionaries are detached from the admitted semantic record.
Passthrough resolution combines inherited columns and declared additions before publishing a configured schema.

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
           "viewer": None,
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
- The ``_passthrough`` marker means upstream columns are inherited.
  Added declared fields must retain their type/image/viewer metadata; the marker is not a substitute for those fields.

For tools with **dynamic** output columns (``Generate``, the merge
tools), the static schema only covers fields declared on ``Outputs``.
The actually-resolved schema for a configured node is available via
:func:`~bioimageflow.validation.serialize_resolved_outputs(node)` —
useful for rendering per-column output pins.
Concrete dynamic DataFrameTools need no Outputs declaration or worker environment.
Unknown or unresolved schemas stay explicitly unresolved; do not invent available columns or treat an empty static schema as complete configured-node knowledge.
Passthrough added-field preservation and truthful unresolved diagnostics are accepted target contracts assessed by later test/code review, not conformance proved by this guide.

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
when declaration inspection cannot produce a wire-format schema; inspection does not require tool instantiation.
Direct callers should surface the declaration failure.
Registry discovery may continue for other classes, but the accepted contract preserves an unavailable/invalid schema diagnostic instead of advertising an empty successful schema.
The exact current error representation is assessed during test/code review; no additional exception or status field is introduced here.
