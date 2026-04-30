Logging
=======

BioImageFlow uses the standard ``logging`` module under two logger
names:

- ``bioimageflow`` — engine, registry, environment management,
  template resolution, tool loading.
- ``wetlands`` — worker process output (stdout/stderr from
  ``ProcessingTool.process_row`` runs).

The first :class:`~bioimageflow.engine.DefaultEngine` instantiation
attaches a :class:`logging.StreamHandler` to each logger if none are
present, with the format
``%(asctime)s [%(name)s] %(message)s``, datefmt ``%H:%M:%S``, and
level ``INFO``. Existing handlers are left in place — applications
that configure logging before importing BioImageFlow keep their
configuration.

Adjusting verbosity
-------------------

The standard ``logging`` API works:

.. code-block:: python

   import logging
   logging.getLogger("bioimageflow").setLevel(logging.DEBUG)
   logging.getLogger("wetlands").setLevel(logging.WARNING)

Routing to a file
-----------------

Replace or supplement the default ``StreamHandler``:

.. code-block:: python

   import logging
   handler = logging.FileHandler("bioimageflow.log")
   handler.setFormatter(
       logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
   )
   logging.getLogger("bioimageflow").addHandler(handler)

Worker logs
-----------

Worker process output is forwarded to the ``wetlands`` logger by the
Wetlands runtime. Hosts that surface execution logs in a UI typically
attach a custom handler to that logger and pump records onto a
dispatch queue.

Specs.md §11 covers the contract end-to-end.
