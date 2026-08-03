Logging
=======

BioImageFlow uses the standard ``logging`` module under two logger
names:

- ``bioimageflow`` — engine, registry, environment management,
  template resolution, tool loading.
- ``wetlands`` — worker process output (stdout/stderr from
  ``ProcessingTool.process_row`` runs).

BioImageFlow does not configure console logging during engine or
workflow construction. Library users that want terminal progress can
call :func:`bioimageflow.configure_logging` from the host application:

.. code-block:: python

   import bioimageflow

   bioimageflow.configure_logging()

By default this sets the ``bioimageflow`` logger level to ``INFO`` and
routes emitted ``DEBUG`` / ``INFO`` records to stdout and ``WARNING`` /
``ERROR`` / ``CRITICAL`` records to stderr, using the format
``%(asctime)s [%(name)s] %(message)s``. Pass ``level=logging.DEBUG`` to
emit debug records. Repeated calls are idempotent for BioImageFlow-owned
console handlers; ``force=True`` replaces only those handlers and
leaves unrelated user handlers in place.

``configure_logging()`` also delegates Wetlands console setup to ``wetlands.logger.enable_console_logging``.
The local worker runtime forwards worker stdout and stderr through the Wetlands logging channel.

Adjusting verbosity
-------------------

The standard ``logging`` API works:

.. code-block:: python

   import logging
   logging.getLogger("bioimageflow").setLevel(logging.DEBUG)
   logging.getLogger("wetlands").setLevel(logging.WARNING)

Routing to a file
-----------------

Add your own handlers with the standard ``logging`` API:

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
Wetlands runtime. Wetlands owns its file logging through its
environment manager; BioImageFlow does not duplicate or wrap that file
logging behavior. Hosts that surface execution logs in a UI typically
attach a custom handler to the ``wetlands`` logger and pump records
onto a dispatch queue.

Specs.md §11 covers the contract end-to-end.
