Submitted Execution
===================

Submitted execution runs the BioImageFlow orchestrator in a separate Python process.
The process that submits the workflow may then close while the workflow continues.
A later process can reconnect using the run ID to read progress, cancel the run, or retrieve its result.

This mode is useful for:

- a long local run that should survive after a GUI closes;
- a service that starts workflows in separate processes;
- a workflow submitted to a cluster.

If none of these apply, ordinary ``workflow.compute()`` is simpler.

What runs where
---------------

A submitted run has three participants:

1. The **submitting process** prepares the workflow and asks BioImageFlow to start it.
2. The **BioImageFlow orchestrator** runs separately, interprets the workflow graph, manages the cache, and records progress and results.
3. **Parsl workers** execute the ProcessingTool tasks selected by the orchestrator.

Parsl is a Python library for sending tasks to worker pools.
BioImageFlow uses it here because the same submitted-run design must work with local workers and cluster workers.
Parsl handles task delivery and worker resources; BioImageFlow remains responsible for workflow meaning, caching, provenance, deterministic failures, and results.

PSI/J is not needed for a submitted run on the same machine.
It is introduced only when BioImageFlow must ask a cluster scheduler to start the orchestrator; see :doc:`remote_cluster`.

Why configuration is an importable function
--------------------------------------------

A normal attached Parsl engine can receive an already-created ``parsl.Config`` Python object because the caller and engine live in the same process.
A submitted orchestrator lives in another process, or possibly on another computer, so it cannot access that in-memory object.

BioImageFlow also deliberately does not pickle and transfer a Config.
A Config may contain Python classes, callbacks, providers, open resources, and site-specific state.
Pickling executable Python objects across a process or machine would be brittle, difficult to validate, and unsafe for an untrusted submission.

Instead, the orchestrator imports a small trusted function that creates the Config in the process where it will be used.
The Config describes Parsl's executors, worker providers, and related runtime behavior.
:class:`~bioimageflow.ParslConfigRef` describes that function and its ordinary data arguments.

This function is sometimes called a **factory**, meaning an ordinary function whose job is to create and return an object.
It must be trusted because importing and calling Python code can perform arbitrary actions.
An administrator or application therefore decides which configuration functions are allowed rather than accepting an arbitrary function name from an untrusted user.

Understand the ``module:function`` name
---------------------------------------

The string ``"my_application.parsl_config:build"`` means:

- import the Python module ``my_application.parsl_config``;
- find the function named ``build`` in that module;
- call it with the supplied keyword arguments.

For example, an installed project could contain this file:

.. code-block:: text

   my_application/
   ├── __init__.py
   └── parsl_config.py

Its ``parsl_config.py`` could define a local Parsl executor:

.. code-block:: python

   from parsl import Config
   from parsl.executors import ThreadPoolExecutor

   def build(*, max_threads: int) -> Config:
       return Config(
           executors=[
               ThreadPoolExecutor(
                   label="local-threads",
                   max_threads=max_threads,
               )
           ],
           retries=0,
       )

The submitting code refers to that function without calling it:

.. code-block:: python

   from bioimageflow import ParslConfigRef

   parsl_config = ParslConfigRef(
       "my_application.parsl_config:build",
       {"max_threads": 4},
   )

The module must be installed or otherwise importable by the submitted orchestrator.
For remote execution, it must therefore be available on the cluster, not only on the laptop.

The arguments are restricted to finite JSON-safe values such as strings, numbers, booleans, lists, and dictionaries.
This makes the submission inspectable and stable across processes.

Secrets
-------

Do not put a password, token, or other credential directly in ``kwargs``.
If the trusted factory needs a secret, refer to the name of an environment variable:

.. code-block:: python

   parsl_config = ParslConfigRef(
       "my_application.parsl_config:build",
       {"max_threads": 4},
       secret_refs={"scheduler_token": "MY_SCHEDULER_TOKEN"},
   )

Here ``scheduler_token`` is the factory argument and ``MY_SCHEDULER_TOKEN`` is the environment-variable name.
The value of that environment variable is read only in the process that builds the Config.
It is not placed in the serialized submission or structured diagnostics.

BioImageFlow validates that the factory is allowed, the named secrets exist, Parsl retries are disabled, and the Config's executor labels match their BioImageFlow descriptions.
Parsl retries must be disabled because BioImageFlow records and controls its own attempts, retries, and deterministic failure selection; an additional hidden Parsl retry would make that history ambiguous.
:func:`~bioimageflow.validate_parsl_config_ref` performs that check in a short-lived process without starting Parsl workers or creating a workflow run.

Submit on the same machine
--------------------------

The smallest submitted example starts the orchestrator as a separate local process:

.. code-block:: python

   from bioimageflow import OrchestratorLaunchConfig, submit_workflow

   run = submit_workflow(
       workflow,
       inputs={"folder": "/data/images"},
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=OrchestratorLaunchConfig(backend="local"),
   )

``submit_workflow()`` returns a :class:`~bioimageflow.WorkflowRun` immediately after preparing and starting the separate process.
``bindings`` describes what the ``local-threads`` executor can run; :doc:`routing` introduces bindings with examples.

The orchestrator's standard output and error are captured as run logs.
The workflow's explicit ``storage_path`` contains the durable run state, cache, progress, and results.
The storage path is runtime configuration and is not written into the reusable workflow definition.

Choose inputs or target nodes
-----------------------------

There are two ways to describe what the submitted orchestrator should compute.
Use only one for a given run:

**Call the workflow's public interface**
   Pass ``inputs={...}`` and omit ``targets``.
   This is the usual choice for a reusable workflow with declared inputs and outputs.

**Request existing graph nodes**
   Pass their registered names in ``targets=[...]`` and omit ``inputs``.
   This is the submitted equivalent of ``workflow.compute(first_node, second_node)``.

DataFrame inputs are stored in the run and checked with content digests before the orchestrator starts Parsl.

Reconnect to the run
--------------------

Save ``run.id`` after submission.
A later process on the same machine or shared filesystem can reopen the run:

.. code-block:: python

   from bioimageflow import WorkflowRun

   run = WorkflowRun.open(workflow.storage_path, saved_run_id)
   run.refresh()

   print(run.status)
   events = run.progress(after_sequence=0)
   failures = run.diagnostics()
   text = run.logs()

   if run.status == "succeeded":
       result = run.load_result()

Call ``refresh()`` periodically until the local run reaches a terminal state.
A remote run handle additionally provides ``wait()`` as a polling convenience.

The normal lifecycle is ``prepared`` → ``starting`` → ``running`` → ``finalizing`` → ``succeeded``.
Other terminal states are ``failed``, ``cancelled``, and ``lost``.
``lost`` means BioImageFlow can no longer prove that the orchestrator finished its normal cleanup.

Cancel a run
------------

``run.cancel()`` records a durable cancellation request.
An active orchestrator stops submitting new tasks, asks Parsl to cancel outstanding work, waits for its writers and tasks to settle, and then records ``cancelled``.

``hard_cancel_after`` is an optional grace period for an orchestrator that does not respond to normal cancellation.
After that period BioImageFlow may terminate the exact recorded process or scheduler job.
Such a run becomes ``lost``, because forced termination cannot prove that normal cleanup happened.

Manual process launch
---------------------

``OrchestratorLaunchConfig(backend="manual")`` prepares the run and writes a shell-free ``command.json`` instead of starting a process.
The run remains ``prepared`` until a trusted external service executes that exact command.
Most users do not need this mode; it exists for applications that already operate their own process-launch service.

Results and retries
-------------------

``run.load_result()`` reads the exact result retained for the successful run.
``run.export_result(destination)`` creates an immutable portable result bundle; see :doc:`results`.

``run.plan_retry()`` previews a retry or recomputation without changing storage.
After confirmation, ``run.start_retry(plan)`` creates a related run; see :doc:`retries`.

Advanced storage guarantees
---------------------------

BioImageFlow installs the complete public return before it marks a run as successful.
The return preserves DataFrame or ordered-mapping shape, output-node identities, immutable record assets, declared external paths, and transient assets needed by the result.
If a required immutable record was explicitly pruned or became corrupted, ``load_result()`` raises :class:`~bioimageflow.WorkflowRunResultUnavailableError`.

Mutable launcher state, logs, transported inputs, and result transfers do not affect cache identities or immutable cache records.
The complete storage and retention contract is documented in :doc:`/reference/output_cache_storage`.
