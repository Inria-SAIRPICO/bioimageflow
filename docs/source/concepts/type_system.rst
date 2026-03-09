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

:class:`~bioimageflow_core.GUIMeta` attaches GUI hints to input fields using
the same ``Annotated`` mechanism as ``ImageSpec``. A separate GUI can
introspect these hints to render appropriate widgets.

.. code-block:: python

   from typing import Annotated
   from bioimageflow_core import GUIMeta, ImageSpec, Semantic

   class Inputs(IOModel):
       image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
       diameter: Annotated[float, GUIMeta(connectable=False, min=1.0, max=500.0, step=0.5)] = 30.0

Parameters:

- **connectable** (``bool``, default ``True``): whether the input can be bound
  to an upstream column. Set to ``False`` for pure user-parameters that should
  only appear as widgets (sliders, spinboxes), not as connectable ports.
- **min** / **max** (``float | None``): numeric bounds for the widget.
- **step** (``float | None``): step increment for spinbox or slider widgets.

Fields without ``GUIMeta`` default to ``connectable=True`` --- existing tools
work unchanged.

``GUIMeta`` and ``ImageSpec`` can coexist on the same field:

.. code-block:: python

   image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(connectable=True)]

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
   #         "connectable": False,
   #         "image_spec": None,
   #         "min": 1.0,
   #         "max": 500.0,
   #         "step": 0.5,
   #     },
   #     ...
   # }
