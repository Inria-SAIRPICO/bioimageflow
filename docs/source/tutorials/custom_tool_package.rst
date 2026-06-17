Creating a Custom Tool Package
==============================

Use a custom tool package when tools should be reused across workflows, versioned independently, distributed to other users, or shipped with package-owned documentation and tests.
For one-project tools that only need to travel with a single workflow, use workflow-local custom tools from :doc:`custom_tool` instead.

Package layout
--------------

A tool package is a normal Python package.
Keep code, tests, fixtures, examples, and docs in the same package boundary:

.. code-block:: text

   my-tools/
     pyproject.toml
     README.md
     my_tools/
       __init__.py
       segment.py
       measure.py
     tests/
       test_segment.py
       data/
         tiny_image.tif
     docs/
       index.md
       tools/
         segment_cells.md
         measure_objects.md
       workflows/
         cell_measurement.md
       images/
         README.md

Use a hyphenated distribution name and an underscored import package name.
For example, the distribution ``my-tools`` imports as ``my_tools``.

.. code-block:: toml

   [project]
   name = "my-tools"
   version = "0.1.0"
   requires-python = ">=3.10"
   dependencies = ["bioimageflow-core"]

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

Processing tools should import only the standard library and ``bioimageflow-core`` at module import time.
Import worker-only libraries such as scikit-image, TensorFlow, PyTorch, or native command wrappers inside ``process_row`` or ``process_batch``.
This keeps schema discovery, documentation builds, and workflow loading independent from heavy runtime environments.

Always use relative imports inside the package:

.. code-block:: python

   # my_tools/__init__.py
   from .segment import SegmentCells
   from .measure import MeasureObjects

Absolute intra-package imports can mix code from different package versions when BioImageFlow loads versioned packages side by side.

Documentation
-------------

Every package should include a short ``docs/index.md`` that explains:

- the package purpose and workflow domain;
- install-time dependencies versus isolated ``EnvironmentSpec`` runtimes;
- public tools grouped by process, not by upstream library;
- public workflow examples;
- how to run package-local tests.

Each public tool should have one Markdown page under ``docs/tools/``.
Document the tool's purpose, inputs, outputs, assumptions, expected results, minimal workflow usage, and expected failure modes.

Each public workflow should have one Markdown page under ``docs/workflows/``.
Document the dataset or fixture, required package tools, expected outputs, and validation metric.
Use tiny generated fixtures whenever possible; commit small fixtures only when they represent real file-layout behavior that synthetic arrays cannot cover.

Custom package docs can opt into a local BioImageFlow docs build with metadata in ``pyproject.toml``:

.. code-block:: toml

   [tool.bioimageflow.docs]
   title = "My Tools"
   root = "docs"
   index = "docs/index.md"
   include_in_main_docs = true
   documentation_url = "https://example.org/my-tools/"

For local docs builds, set ``BIOIMAGEFLOW_DOC_PACKAGE_PATHS`` to one or more package directories separated by the platform path separator before running the tool package docs generator.
If ``documentation_url`` is set for an external package, the main docs can link to that package's own published documentation instead of including local files.

Tests
-----

Ship tests with the package source.
For each public tool, cover:

- schema serialization for ``Inputs`` and ``Outputs``;
- valid and invalid construction or binding behavior;
- one successful execution path with tiny synthetic data or committed fixtures;
- output template resolution and returned ``Outputs`` instances;
- expected failures for invalid parameters, missing files, unsupported layouts, or missing external resources.

For each public workflow, cover graph construction, declared outputs, serialization round trips when supported, and one end-to-end execution on tiny public or synthetic data.
Network-dependent or external-binary tests should not be the only proof for a tool; add deterministic tests around parsing, planning, command generation, and error handling first.

Run package-local tests directly during development:

.. code-block:: bash

   uv run pytest path/to/my-tools/tests

First-party BioImageFlow packages also participate in repository-wide package-tool checks:

.. code-block:: bash

   uv run pytest -m package_tools

Including Package Docs in BioImageFlow
--------------------------------------

The main BioImageFlow docs do not import tool packages while building documentation.
They scan package-owned Markdown files and generate wrapper pages under ``docs/source/tool_packages``.

For first-party packages, run:

.. code-block:: bash

   uv run python docs/generate_tool_package_docs.py
   uv run sphinx-build -W --keep-going docs/source docs/_build/html

For custom packages in a local docs build, configure the package path first:

.. code-block:: bash

   export BIOIMAGEFLOW_DOC_PACKAGE_PATHS=/path/to/my-tools
   uv run python docs/generate_tool_package_docs.py
   uv run sphinx-build -W --keep-going docs/source docs/_build/html

Generated wrapper pages are navigation glue only.
Edit package docs in the package's own ``docs/`` directory, then regenerate the wrappers.
