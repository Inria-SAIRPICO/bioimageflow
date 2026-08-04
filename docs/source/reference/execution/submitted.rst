Submitted Execution
===================

Submitted execution starts the BioImageFlow orchestrator separately from the calling process and persists enough state to reconnect, inspect progress, cancel, and retrieve the exact result.
It always uses Parsl for ProcessingTool dispatch.

Use :func:`~bioimageflow.submit_workflow` for submitted execution.
The workflow keeps its explicit ``storage_path`` as the authoritative runtime root; that path is launcher metadata and is not serialized into the workflow graph or archive.

Trusted Parsl configuration
---------------------------

A submitted run cannot serialize a live Parsl Config, executor, provider, callable, or credential.
It uses an importable :class:`~bioimageflow.ParslConfigRef` instead:

.. code-block:: python

   from bioimageflow import ParslConfigRef

   parsl_config = ParslConfigRef(
       "my_application.parsl_config:build",
       {"profile": "production", "worker_count": 8},
       secret_refs={"scheduler_token": "MY_SCHEDULER_TOKEN"},
   )

The factory receives finite JSON-safe ``kwargs``.
``secret_refs`` maps factory argument names to environment-variable names; secret values are resolved only in the process that invokes the factory.
Literal secret-looking arguments are rejected and secret values are excluded from serialized configuration, representations, and structured diagnostics.

The factory is resolved through a trusted import boundary.
Its Config must use ``retries=0`` and expose exactly the executor labels described by the supplied bindings.
:func:`~bioimageflow.validate_parsl_config_ref` performs this validation in an isolated process, reports sanitized diagnostics, and cleans the process without creating a DataFlowKernel or run.

Submit on the same machine
--------------------------

.. code-block:: python

   from bioimageflow import OrchestratorLaunchConfig, submit_workflow

   run = submit_workflow(
       workflow,
       inputs={"folder": "/data/images"},
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=OrchestratorLaunchConfig(backend="local"),
   )

The local backend starts ``python -m bioimageflow.launcher.orchestrator`` as a separate process with confined stdout and stderr logs.

The ``manual`` backend writes a shell-free ``command.json`` descriptor and leaves the run in ``prepared`` until an external actor executes that exact command:

.. code-block:: python

   launch = OrchestratorLaunchConfig(backend="manual")

The manual backend is useful when another trusted service owns process launch but BioImageFlow must still own run state and invocation semantics.

Choose inputs or targets
------------------------

One submitted invocation uses exactly one public workflow calling convention:

- pass ``inputs={...}`` and omit ``targets`` to call the workflow's public root interface;
- pass immediate registered node names in ``targets=[...]`` and omit ``inputs`` to reproduce ``workflow.compute(*targets)``.

Root DataFrame inputs are externalized below the launcher control directory and verified by both a Parquet transport digest and a canonical logical DataFrame digest before Parsl starts.

Reconnect to a local run
------------------------

.. code-block:: python

   from bioimageflow import WorkflowRun

   run = WorkflowRun.open(workflow.storage_path, saved_run_id)
   run.refresh()
   events = run.progress(after_sequence=0)
   diagnostics = run.diagnostics()
   text = run.logs()

   if run.status == "succeeded":
       result = run.load_result()

Call ``refresh()`` periodically until a local run reaches a terminal state.
:class:`~bioimageflow.RemoteWorkflowRun` additionally provides a bounded polling ``wait()`` convenience method.

The durable states are ``prepared``, ``starting``, ``running``, ``finalizing``, ``cancel_requested``, ``succeeded``, ``failed``, ``cancelled``, and ``lost``.
``status.json`` below ``launcher/v1/runs/<run-id>/`` is authoritative for reconnection.

Successful submission persists the exact public return before marking the run successful.
The return preserves the single DataFrame or ordered mapping shape, exact output node identities, immutable record assets, declared external paths, and self-contained transient assets.
If an explicitly pruned or corrupted immutable record is needed, ``load_result()`` raises :class:`~bioimageflow.WorkflowRunResultUnavailableError`.

Result export and retained retries
----------------------------------

Use ``run.export_result(destination)`` for an immutable portable result bundle; the full integrity and asset-rehydration contract is in :doc:`results`.
Use ``run.plan_retry()`` for a non-mutating retry or recomputation preview and ``run.start_retry(plan)`` after confirmation; terminal-state rules, revision binding, restart-safe confirmation, and uncertainty handling are in :doc:`retries`.

Cancellation and hard termination
---------------------------------

``run.cancel()`` cancels an unclaimed prepared run directly or durably requests cancellation from an active orchestrator.
The orchestrator stops new task submission, asks Parsl to cancel outstanding work, drains writers and futures, and finishes as ``cancelled`` when cleanup is proven.

``hard_cancel_after`` is an optional positive grace period on local and PSI/J launch configurations.
If a persisted process or scheduler job remains unresponsive after that period, a reconnected handle may terminate that exact launch identity.
Confirmed forced termination becomes ``lost`` because normal cleanup cannot be claimed.

Cluster launch and SSH transport
--------------------------------

:class:`~bioimageflow.PSIJLaunchConfig` submits exactly one scheduler job for the orchestrator.
Parsl providers remain responsible for worker allocations.
See :doc:`remote_cluster` for PSI/J settings, pre-launch initialization, immutable uploads, remote profile testing, and transport-backed reconnect.

Storage and retention
---------------------

Mutable launcher state, logs, externalized inputs, and return transports do not enter cache identities or canonical cache records.
The complete storage and retention contract is documented in :doc:`/reference/output_cache_storage`.
