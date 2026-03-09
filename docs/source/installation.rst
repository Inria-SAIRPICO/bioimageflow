Installation
============

Requirements
------------

- Python >= 3.13

Install from PyPI
-----------------

.. code-block:: bash

   pip install bioimageflow

This installs both ``bioimageflow`` (the orchestrator) and ``bioimageflow-core``
(the zero-dependency core).

Install for development
-----------------------

Clone the repository and use `uv <https://docs.astral.sh/uv/>`_ to set up the
workspace:

.. code-block:: bash

   git clone https://github.com/your-org/bioimageflow.git
   cd bioimageflow
   uv sync

This installs both packages in editable mode along with development dependencies
(pytest, sphinx, etc.).

Package structure
-----------------

BioImageFlow is split into two packages:

``bioimageflow-core``
   Zero external dependencies. Contains types, tool base classes, and
   shared-memory utilities. Installed in both the main process and all worker
   environments.

``bioimageflow``
   Depends on pandas and pydantic. Contains the workflow engine, DAG
   construction, caching, and execution logic. Only needed in the main process.

Running tests
-------------

.. code-block:: bash

   uv run pytest                    # all tests
   uv run pytest tests/unit/        # unit tests only
   uv run pytest tests/integration/ # integration tests only
