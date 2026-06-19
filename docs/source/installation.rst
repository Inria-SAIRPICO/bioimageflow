Installation
============

Requirements
------------

- Python >= 3.10

BioImageFlow's deterministic CI test matrix runs on Python 3.10, 3.11, and 3.12.
The worker-safe ``bioimageflow-core`` package supports Python >= 3.9 because it is installed into isolated Wetlands worker environments, including external-binary environments whose dependencies require Python 3.9.

Install from PyPI
-----------------

.. code-block:: bash

   pip install bioimageflow

or in a `uv <https://docs.astral.sh/uv/>`_ project:

.. code-block:: bash

   uv add bioimageflow

This installs both ``bioimageflow`` (the orchestrator) and ``bioimageflow-core``
(the worker-safe core).

Companion tool packages
-----------------------

The documentation imports source and processing tools (``Files``, ``Generate``,
``ExtractChannel``, ``Mosaic``, the merge tools, ...) from a layered
companion package, ``bioimageflow-common-tools``. Installing it alongside the
core library lets every example in the docs run without copy-pasting helper
classes:

.. code-block:: bash

   pip install bioimageflow-common-tools

The package is not part of the core surface; it is a curated set of basic
tools built on top of ``bioimageflow`` and ``bioimageflow-core``. Workflow
authors are free to use it directly, write their own tools following the same
patterns, or mix both.

Image IO, segmentation, measurement, restoration, spot analysis, tracking, and
SAIRPICO wrappers live in separate optional companion packages so heavier domain
dependencies do not become canonical common-tool exports. Install the packages
needed by the workflows you run:

.. code-block:: bash

   pip install bioimageflow-io-tools
   pip install bioimageflow-measurement-tools
   pip install bioimageflow-segmentation-tools
   pip install bioimageflow-sairpico-tools
   pip install bioimageflow-spot-tools
   pip install bioimageflow-restoration-tools
   pip install bioimageflow-tracking-tools

The tutorials that import ``bioimageflow_io_tools`` or
``bioimageflow_segmentation_tools`` require those corresponding packages in
addition to ``bioimageflow-common-tools``.

Install for development
-----------------------

Clone the repository and use `uv <https://docs.astral.sh/uv/>`_ to set up the
workspace:

.. code-block:: bash

   git clone https://gitlab.inria.fr/sairpico/bioimageflow.git
   cd bioimageflow
   uv sync

This installs all workspace packages in editable mode along with development
dependencies (pytest, sphinx, etc.).

Package structure
-----------------

BioImageFlow is split into two packages:

``bioimageflow-core``
   Minimal worker-safe dependencies. Contains types, tool base classes, and
   shared-memory utilities. It declares NumPy because shared-memory helpers
   expose NumPy array views. Installed in both the main process and all worker
   environments.

``bioimageflow``
   Depends on pandas and pydantic. Contains the workflow engine, DAG
   construction, caching, and execution logic. Only needed in the main process.

Running tests
-------------

See :doc:`/reference/testing` for the regular and complete test workflow.

.. code-block:: bash

   uv run pytest                    # regular tests
   uv run pytest tests/unit/        # unit tests only
   uv run pytest tests/integration/ # integration tests only
