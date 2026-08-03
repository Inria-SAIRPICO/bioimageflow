Installation
============

Requirements
------------

- Python >= 3.10

BioImageFlow's deterministic CI test matrix runs full fast coverage on Python 3.10 and 3.12, plus Python 3.11 compatibility smoke on every pipeline.
Full deterministic Python 3.11 validation is manually available before tagging and rerun as a required release gate.
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

Parsl and remote cluster execution
----------------------------------

Install the Parsl runtime for attached or separate-process execution:

.. code-block:: bash

   pip install "bioimageflow[parsl]"

Laptop-to-cluster execution additionally requires the system ``ssh`` and ``sftp`` clients from OpenSSH on the laptop.
Authentication, host aliases, users, ports, keys, agents, ``ProxyJump``, and host-key policy belong in the user's normal OpenSSH configuration.
BioImageFlow does not accept passwords, private-key contents, arbitrary SSH options, remote shell setup, or installation commands.

Prepare the cluster environment independently:

.. code-block:: bash

   pip install "bioimageflow[parsl,psij]"

Install the PSI/J executor plugin supplied for the site's Slurm, PBS, or LSF scheduler in that same environment.
The selected executor descriptor and scheduler client commands must be available on the login node.
Workflow configuration factories and worker tool environments must also be installed or available through their declared shared source paths.
No hostname, account, queue, credential, home directory, or scheduler command has a library default.

Start with :doc:`/how-to/remote_cluster` for a first submission.
The complete technical contract is in :doc:`/reference/execution/index`, and :doc:`/gui/submitted_parsl` documents integration into a BioImageFlow GUI.

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

The how-to guides that import ``bioimageflow_io_tools`` or
``bioimageflow_segmentation_tools`` require those corresponding packages in
addition to ``bioimageflow-common-tools``.

Install for development
-----------------------

Clone the repository and use `uv <https://docs.astral.sh/uv/>`_ to set up the
workspace:

.. code-block:: bash

   git clone git@github.com:Inria-SAIRPICO/bioimageflow.git
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
