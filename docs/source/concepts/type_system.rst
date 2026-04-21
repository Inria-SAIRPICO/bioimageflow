Type System
===========

BioImageFlow uses Python's ``Annotated`` types to describe image data flowing
through the pipeline. The type system catches incompatibilities at
graph-construction time, before any computation runs.

ImageSpec
---------

:class:`~bioimageflow_core.ImageSpec` is a frozen dataclass with four optional
constraint sets:

.. code-block:: python

   @dataclass(frozen=True)
   class ImageSpec:
       semantics: frozenset[str] = frozenset()  # what pixels represent
       layouts: frozenset[str] = frozenset()     # axis ordering
       dtypes: frozenset[str] = frozenset()      # numpy dtypes
       formats: frozenset[str] = frozenset()     # "memory" or file formats

An empty set means **wildcard** --- any value is accepted.

Semantic
--------

:class:`~bioimageflow_core.Semantic` describes what the pixel values represent:

.. list-table::
   :header-rows: 1

   * - Value
     - Meaning
   * - ``BINARY``
     - Binary mask (0/1)
   * - ``LABEL``
     - Instance or semantic label map (integers)
   * - ``INTENSITY``
     - Raw intensity image
   * - ``PROBABILITY``
     - Probability map (0.0--1.0)
   * - ``DISPLACEMENT``
     - Vector field / displacement map
   * - ``FEATURE``
     - Feature map (e.g., embeddings)

Layout
------

:class:`~bioimageflow_core.Layout` describes the axis ordering:

.. list-table::
   :header-rows: 1

   * - Value
     - Axes
     - ``ndim``
   * - ``PLANAR``
     - YX
     - 2
   * - ``PLANAR_CHANNEL``
     - CYX
     - 3
   * - ``PLANAR_TIME``
     - TYX
     - 3
   * - ``PLANAR_TIME_CHANNEL``
     - TCYX
     - 4
   * - ``VOLUMETRIC``
     - ZYX
     - 3
   * - ``VOLUMETRIC_CHANNEL``
     - CZYX
     - 4
   * - ``VOLUMETRIC_TIME``
     - TZYX
     - 4
   * - ``VOLUMETRIC_TIME_CHANNEL``
     - TCZYX
     - 5

ImagePath and ImageShared
-------------------------

These are factory functions that produce ``Annotated`` types:

.. code-block:: python

   from bioimageflow_core import ImagePath, ImageShared

   # File-based image: Annotated[Path, ImageSpec(...)]
   image: ImagePath(semantics={"intensity"}, layouts={"YX", "CYX"})

   # Shared-memory image: Annotated[SharedArray, ImageSpec(..., formats={"memory"})]
   image: ImageShared(semantics={"intensity"})

Use :func:`~bioimageflow_core.ImagePath` for file-based I/O and
:func:`~bioimageflow_core.ImageShared` for zero-copy shared memory.

Compatibility checking
----------------------

:func:`~bioimageflow_core.check_compatibility` validates that a producer's
output type is compatible with a consumer's input type. The rules use
**asymmetric wildcard semantics**:

1. **Empty consumer set** = accept anything (wildcard)
2. **Empty producer set + non-empty consumer set** = warn but accept
3. **Both non-empty** = sets must intersect

.. code-block:: python

   from bioimageflow_core import ImageSpec, check_compatibility

   producer = ImageSpec(semantics=frozenset({"label"}))
   consumer = ImageSpec(semantics=frozenset({"label", "binary"}))

   check_compatibility(producer, consumer)  # True: {"label"} & {"label", "binary"} = {"label"}

   consumer_strict = ImageSpec(semantics=frozenset({"binary"}))
   check_compatibility(producer, consumer_strict)  # False: {"label"} & {"binary"} = {}

This checking happens automatically when you bind a column reference to an
input --- you don't need to call it manually.

GUIMeta
-------

:class:`~bioimageflow_core.GUIMeta` attaches GUI hints to ``Inputs`` and
``Outputs`` fields using the same ``Annotated`` mechanism as ``ImageSpec``.
A separate GUI can introspect these hints to render appropriate widgets,
labels, and tooltips.

.. code-block:: python

   from typing import Annotated
   from bioimageflow_core import Connectable, GUIMeta, ImageSpec, Semantic

   class Inputs(IOModel):
       image: Annotated[
           Path,
           ImageSpec(semantics={Semantic.INTENSITY}),
           GUIMeta(
               display_name="Input image",
               description="Fluorescence image to segment.",
               connectable=Connectable.BY_DEFAULT,
           ),
       ]
       diameter: Annotated[float, GUIMeta(
           display_name="Cell diameter",
           description="Approximate cell diameter, in pixels.",
           min=1.0, max=500.0, step=0.5,
       )] = 30.0

Parameters:

- **display_name** (``str | None``): human-readable label shown in the GUI.
  When ``None``, frontends fall back to the field name.
- **description** (``str | None``): longer help text for tooltips / inline
  help, describing what the field means and when to change it.
- **connectable** (:class:`~bioimageflow_core.Connectable`, default
  ``Connectable.NOT_BY_DEFAULT``): controls pin visibility for ``Inputs``
  fields. ``NEVER`` hides the pin entirely, ``NOT_BY_DEFAULT`` shows it only
  when toggled via checkbox, ``BY_DEFAULT`` shows it out of the box. Ignored
  for ``Outputs`` fields, which always expose a pin.
- **min** / **max** (``float | None``): numeric bounds for the widget.
- **step** (``float | None``): step increment for spinbox or slider widgets.
- **group** (``str | None``): logical group name for tabs or sections
  (e.g. ``"general"``, ``"advanced"``, ``"gpu"``).

Fields without ``GUIMeta`` default to ``Connectable.NOT_BY_DEFAULT`` with no
label, description, numeric bounds, or group. Data input fields (image paths)
should use explicit ``GUIMeta(connectable=Connectable.BY_DEFAULT)`` to show
their pins.

Output fields can also carry ``GUIMeta`` so the GUI can label output pins and
show tooltips:

.. code-block:: python

   class Outputs(IOModel):
       mask: Annotated[
           Path,
           ImageSpec(semantics={Semantic.LABEL}),
           GUIMeta(display_name="Segmentation mask",
                   description="Label image; each cell gets a unique ID."),
       ] = Path("{input_image.stem}_mask{ext}")
       cell_count: Annotated[int, GUIMeta(
           display_name="Cell count",
           description="Number of cells detected.",
       )]

``GUIMeta`` and ``ImageSpec`` can coexist on the same field:

.. code-block:: python

   image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(connectable=Connectable.BY_DEFAULT)]

Introspection
~~~~~~~~~~~~~

Use :func:`~bioimageflow.validation.get_inputs_schema` to retrieve a
GUI-friendly schema for a tool:

.. code-block:: python

   from bioimageflow import get_inputs_schema

   schema = get_inputs_schema(my_tool)
   # {
   #     "diameter": {
   #         "type": float,
   #         "default": 30.0,
   #         "required": False,
   #         "connectable": Connectable.NOT_BY_DEFAULT,
   #         "image_spec": None,
   #         "display_name": "Cell diameter",
   #         "description": "Approximate cell diameter, in pixels.",
   #         "min": 1.0,
   #         "max": 500.0,
   #         "step": 0.5,
   #     },
   #     ...
   # }
