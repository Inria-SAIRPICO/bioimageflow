bioimageflow.validation
=======================

The :mod:`bioimageflow.validation` module exposes the public validation
surface — error types, schema serializers, and single-field helpers.
This page is a curated reference: only the public entry points are
listed, with one paragraph and one example each. The full
``ValidationErrorKind`` table is in :doc:`/reference/errors`.

Error types
-----------

.. py:class:: ValidationError

   Dataclass for a single validation problem. Fields: ``kind``,
   ``message``, ``node``, ``field``, ``edge``, ``edge_id``, ``path``.
   See :doc:`/reference/errors` for the full table.

.. py:data:: ValidationErrorKind

   ``Literal[...]`` alias enumerating every legal value of
   ``ValidationError.kind``. Hosts that route errors by kind should
   match against this set.

.. py:exception:: SchemaSerializationError

   Raised by :func:`serialize_input_schema` /
   :func:`serialize_output_schema` when the tool class cannot be
   inspected.
   Serializers inspect resolved class declarations without instantiating the tool; unavailable annotations must remain actionable diagnostics.

Single-node and single-field helpers
------------------------------------

.. py:function:: validate_parameters(tool_class, parameters, *, node=None)

   Validate a dict of constants against ``tool_class.Inputs`` without
   constructing a workflow. Returns a list of ``ValidationError``
   entries with ``kind="parameter_invalid"``. Only the supplied
   parameters are checked; missing required fields are reported by
   ``Workflow.validate``.

   .. code-block:: python

      from bioimageflow.validation import validate_parameters

      errors = validate_parameters(
          MyTool, {"sigma": 1.5, "iterations": -3}, node="filter",
      )

.. py:function:: check_type_compat(node, field, col_ref)

   Return a ``ValidationError`` (kind ``type_mismatch``) if ``col_ref``
   is incompatible with ``node.<field>``; ``None`` on success. Pure;
   does not raise. Useful for live edge-drag previews.

   .. code-block:: python

      from bioimageflow.validation import check_type_compat

      err = check_type_compat(consumer_node, field="image", col_ref=upstream["mask"])

Schema serialization
--------------------

.. py:function:: serialize_input_schema(tool_class)

   Return a JSON-safe input schema for ``tool_class``. One entry per
   field declared on ``Inputs``, with ``type`` (display string),
   ``required``, ``nullable``, ``connectable``, ``default``, ``min``/``max``/``step``, ``path_picker``,
   ``choices``, ``image_spec``, and the GUIMeta strings. Returns
   ``{}`` when the tool has no ``Inputs``. See :doc:`/gui/tool_schemas`
   for the per-field shape.

   .. code-block:: python

      from bioimageflow.validation import serialize_input_schema

      schema = serialize_input_schema(MyTool)

.. py:function:: serialize_output_schema(tool_class)

   Return a JSON-safe output schema for ``tool_class``. Per-field
   fields follow the complete catalogue in :doc:`/specs` §2.4, including template/viewer metadata.
   No static Outputs declaration produces an empty static schema, not a claim that a dynamic DataFrameTool has no runtime columns.
   A Passthrough marker denotes inherited columns; added declarations must preserve their type/image/viewer metadata.
   Added-field and unavailable-schema conformance remain explicit test/code-review obligations.

   .. code-block:: python

      from bioimageflow.validation import serialize_output_schema

      schema = serialize_output_schema(MyTool)

.. py:function:: serialize_image_spec(spec)

   Convert an :class:`~bioimageflow_core.ImageSpec` (or ``None``) into
   a JSON-safe dict of sorted-string lists with keys ``semantics``,
   ``layouts``, ``dtypes``, ``formats``. Enum values are written as
   strings.

   .. code-block:: python

      from bioimageflow.validation import serialize_image_spec
      from bioimageflow_core import ImageSpec, Semantic

      serialize_image_spec(ImageSpec(semantics={Semantic.INTENSITY}))
      # {"semantics": ["intensity"], "layouts": [], "dtypes": [], "formats": []}

.. py:function:: get_inputs_schema(tool)

   In-process counterpart to :func:`serialize_input_schema`. Returns a
   dict with **live** Python types (raw ``type`` annotations, raw
   :class:`~bioimageflow_core.Connectable` values) instead of
   wire-format strings. Use it when the host is in the same process as
   the tool class and just wants Python objects.

   .. code-block:: python

      from bioimageflow import get_inputs_schema

      schema = get_inputs_schema(my_tool_instance)
      schema["sigma"]["type"]         # <class 'float'>
      schema["sigma"]["connectable"]  # Connectable.NOT_BY_DEFAULT

Constants
---------

.. py:function:: serialize_constant(value)

   Wrap a Python scalar/list/tuple in the wire-format envelope
   ``{"__type__": ..., "value": ...}``. Recognised types: ``bool``,
   ``int``, ``float``, ``str``, ``list``, ``tuple``.
   The accepted portable-constant contract is faithful round-trip for supported values and explicit refusal for unsupported values; silent stringification is not scientific data preservation.
   The exact supported nested grammar and implementation conformance are assessed during test/code review.
   Owner-bound runtime references and invocation-only values are not portable graph constants merely because current worker transport supports them.

   .. code-block:: python

      from bioimageflow.validation import serialize_constant

      serialize_constant(3.14)
      # {"__type__": "float", "value": 3.14}

.. py:function:: deserialize_constant(data)

   Reconstruct the supported portable value from its admitted envelope.
   Inverse/round-trip claims apply only to the explicitly supported constant grammar, not arbitrary values converted to strings.

   .. code-block:: python

      from bioimageflow.validation import deserialize_constant

      deserialize_constant({"__type__": "int", "value": 7})  # 7

Both helpers are used internally by ``Workflow.to_dict`` /
``WorkflowSession.set_constant`` and exposed publicly for hosts that
need to encode constants outside those code paths.

Resolved semantic authority
---------------------------

Configured nodes use a detached resolved port record and publish its portable projection.
The current ``type_spec`` descriptor carries finite primitive, Path, scoped-reference/array, union, collection and literal-choice structure without Python expression evaluation.
Display labels remain presentation data; resolution failure cannot become available empty columns, and known heterogeneous Any remains valid.
Graph constant decoding admits exact variant payload types before any semantic coercion; malformed bool/int/none payloads are refused.
