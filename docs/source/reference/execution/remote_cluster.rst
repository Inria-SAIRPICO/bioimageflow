Remote Cluster Execution
========================

Remote cluster execution submits one reconnectable BioImageFlow orchestrator job through PSI/J and uses Parsl providers to allocate processing workers.
The public SSH transport lets a laptop or service perform the same operation without private cluster-agent commands or direct access to launcher storage.

Architecture
------------

.. code-block:: text

   laptop or service
       │  OpenSSH transport
       ▼
   cluster login node ── PSI/J ──► orchestrator scheduler job
                                      │
                                      └── Parsl providers ──► worker jobs

The cluster agent is a bounded command protocol invoked through the public transport.
Applications use :class:`~bioimageflow.SSHSubmissionTransport`, :class:`~bioimageflow.RemoteWorkflowRun`, and public validation/preparation operations rather than invoking that command directly.
Define the trusted configuration reference as described in :doc:`submitted` and the worker attestations and capacities as described in :doc:`routing`.

Define the transport
--------------------

.. code-block:: python

   from pathlib import PurePosixPath

   from bioimageflow import SSHSubmissionTransport

   transport = SSHSubmissionTransport(
       host="my-hpc",
       staging_root=PurePosixPath(
           "/cluster/project/my-workflow/transport"
       ),
       remote_executable=PurePosixPath(
           "/cluster/apps/bin/bioimageflow-cluster-agent"
       ),
   )

``host`` is an OpenSSH alias or ``user@destination``.
OpenSSH configuration owns users, keys, agents, ports, ``ProxyJump``, and host-key policy.
BioImageFlow uses batch mode and does not accept passwords, private-key contents, host-key bypasses, or arbitrary SSH options.

The staging root must be absolute, visible on the login node, writable, and disjoint from workflow storage.
It holds transfer bundles and content-addressed upload objects, not authoritative launcher or cache state.

Define the orchestrator job
---------------------------

.. code-block:: python

   from datetime import timedelta
   from pathlib import PurePosixPath

   from bioimageflow import PSIJLaunchConfig

   launch = PSIJLaunchConfig(
       executor="slurm",
       walltime=timedelta(hours=2),
       queue="cpu",
       project="BIOIMAGE",
       cpu_cores=4,
       work_dir=PurePosixPath("/cluster/project/orchestrator"),
       hard_cancel_after=300,
   )

Supported PSI/J executor names are ``slurm``, ``pbs``, and ``lsf``.
``walltime`` is always explicit.
Queue, project, CPU count, and optional normalized work directory describe only the orchestrator job, not Parsl worker jobs.

The launch configuration intentionally has no arbitrary directive, environment mapping, shell fragment, or live PSI/J object.
Typed pre-launch scripts cover orchestrator initialization safely, while Parsl provider configuration covers worker initialization.

Validate a profile without submission
-------------------------------------

.. code-block:: python

   from bioimageflow import validate_remote_execution_profile

   report = validate_remote_execution_profile(
       transport=transport,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=launch,
       storage_path="/cluster/project/my-workflow/results",
   )

This operation validates transport connectivity, remote trusted-factory import and invocation, secret environment references, ``retries=0``, actual executor labels, the PSI/J executor, and relevant path syntax or existence.
It creates neither a workflow run nor a scheduler job, and its report records both facts explicitly.
Resources created for validation are cleaned before it returns.

The operation cannot prove future queue availability, worker-node software, or that a scheduler will accept a later job.
Runtime executor preflight performs worker-side checks after allocation.

Initialize the orchestrator
---------------------------

An optional :class:`~bioimageflow.PreLaunchScript` is sourced once by the PSI/J job before starting the orchestrator.
It can load environment modules, initialize Spack, activate Python, set site variables, or change the working directory:

.. code-block:: python

   from pathlib import Path

   from bioimageflow import PreLaunchScript

   inline = PreLaunchScript.from_text(
       """\
       source /etc/profile.d/modules.sh
       module load python/3.12
       """
   )
   uploaded = PreLaunchScript.from_local_file(Path("cluster-init.sh"))
   cluster_owned = PreLaunchScript.from_cluster_file(
       "/shared/site/bioimageflow-init.sh",
       expected_digest=(
           "sha256:0123456789abcdef0123456789abcdef"
           "0123456789abcdef0123456789abcdef"
       ),
   )

Inline text and local files are captured as immutable submission bytes.
A cluster-file value identifies an external cluster source; an optional digest pins its expected bytes.
During confirmed submission, the cluster agent reads and verifies that source and creates a read-only run-owned snapshot.
PSI/J receives only the run-owned snapshot path, never the original source path.

Scripts must be non-empty UTF-8, no larger than 64 KiB, and free of NUL bytes.
Symlinks, special files, concurrent source changes, and digest mismatches are rejected.
A shebang is ignored because the file is sourced, and exports or directory changes persist in the orchestrator.

Pre-launch setup does not initialize Parsl workers.
Use the selected Parsl provider's worker-initialization facility for worker modules, Spack environments, CUDA libraries, containers, and tool environments.
Do not put literal credentials in scripts: run-owned script bytes necessarily contain plaintext and script output belongs to scheduler-controlled logs.

For example, a trusted Parsl factory may configure Slurm workers independently of the orchestrator:

.. code-block:: python

   from parsl import Config
   from parsl.executors import HighThroughputExecutor
   from parsl.providers import SlurmProvider

   def build_parsl_config(*, partition: str) -> Config:
       worker_init = """\
       source /etc/profile.d/modules.sh
       module load cuda/12
       source /shared/spack/share/spack/setup-env.sh
       spack load py-bioimageflow-core
       """
       return Config(
           executors=[
               HighThroughputExecutor(
                   label="gpu-workers",
                   provider=SlurmProvider(
                       partition=partition,
                       worker_init=worker_init,
                       walltime="02:00:00",
                   ),
               )
           ],
           retries=0,
       )

``worker_init`` is site-owned code inside the trusted factory.
It is not accepted as an untrusted serialized BioImageFlow field.
Environment Modules, Spack, Conda, virtual environments, containers, and site-specific setup are all ordinary uses; BioImageFlow does not privilege one package manager.

Input meaning
-------------

Remote path values use explicit types:

- :class:`~bioimageflow.LocalUpload` packages a laptop file or directory;
- an ordinary ``Path`` is preserved as a cluster path and is never probed on the laptop;
- a string remains a string even when it resembles a path;
- a root DataFrame is transported with Parquet and logical digests, and typed ``Path`` cells must be normalized absolute cluster paths.

``LocalUpload`` can be a path-like root input, a path-shaped leaf inside a root list or tuple, or a value in ``node_input_overrides``.
For example, ``{"files": [LocalUpload(first), LocalUpload(second)]}`` preserves an explicit file order, while ``{"path": LocalUpload(directory)}`` preserves a directory tree.
Only ``LocalUpload`` values and inline or local-file pre-launch sources read laptop bytes.
Installed upload objects must remain readable by the orchestrator and relevant workers for the lifetime of every retained run that references them.

Node path discovery and overrides
---------------------------------

:func:`~bioimageflow.inspect_remote_node_paths` recursively reports every unconnected path-shaped node input using stable scoped node paths.
The report includes its single/list/tuple shape, nullability, path-picker hint, current values, and whether those values are already valid normalized cluster paths.
It neither probes the filesystem nor contacts the cluster and declares ``reads_local_files=False`` and ``allocates_resources=False`` in its serializable report.

``submit_workflow()`` and :func:`~bioimageflow.prepare_remote_submission` accept an invocation-only nested mapping:

.. code-block:: python

   node_input_overrides = {
       "files": {
           "path": LocalUpload(Path("./images")),
       },
       "nested/masks": {
           "files": [LocalUpload(path) for path in selected_masks],
       },
   }

The outer key is a scoped tool-node path and the inner key is an input name.
Only path-shaped constant or default inputs can be replaced; connected inputs, workflow boundary nodes, unknown fields, and non-path parameters are rejected.
Ordinary override ``Path`` values must be normalized absolute POSIX cluster paths, while local sources must be explicitly wrapped in ``LocalUpload``.

The laptop validates each target against the live workflow graph before reading any selected upload.
Preparation copies every selected file or directory into the immutable bundle and omits original laptop paths from the request and manifest.
The cluster agent independently validates the target and uploaded tree, applies decoded installed paths to a fresh reconstructed graph, and submits that effective graph without mutating the caller's workflow.
The effective graph retains the content-addressed installed paths, while the prepared manifest binds the request mapping and every uploaded byte through its entry digests and overall bundle digest.

Workflow graph constants use recursive lossless envelopes for ``Path``, list, tuple, and dictionary values.
Consequently explicit constructs such as ``Files(files=[Path(...), Path(...)])`` serialize without converting paths to ambiguous strings.

Prepare immutable bytes before confirmation
-------------------------------------------

Use :func:`~bioimageflow.prepare_remote_submission` when an application must show a confirmation screen after validating the exact bytes that will be submitted:

.. code-block:: python

   from bioimageflow import prepare_remote_submission

   with prepare_remote_submission(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       targets=None,
       parsl_config=parsl_config,
       executor_bindings=bindings,
       launch=launch,
       pre_launch=PreLaunchScript.from_local_file(
           Path("cluster-init.sh")
       ),
       lifetime=1800,
   ) as prepared:
       display_manifest(prepared.manifest.to_dict())
       run = prepared.submit(transport)

Preparation performs no network operation and creates no workflow run or allocation.
Its stable manifest contains relative entry paths, kinds, sizes, SHA-256 digests, and one bundle digest.
Cluster-resident pre-launch files are listed separately as external sources with their path and optional expected digest; their bytes can only be observed and snapshotted on the cluster.
No secret values are included.

The prepared object owns temporary read-only snapshots, submits at most once, and never rereads original root, node-override, or local pre-launch paths.
It rejects changed staged bytes and expired preparations.
``close()`` or context-manager exit removes abandoned or failed local preparation state.

Submit and reconnect
--------------------

Direct remote submission is the shorter path when a separate confirmation boundary is unnecessary:

.. code-block:: python

   from bioimageflow import submit_workflow

   run = submit_workflow(
       workflow,
       inputs={"images": LocalUpload(Path("./images"))},
       parsl_config=parsl_config,
       executor_bindings=bindings,
       shared_runtime_root="/cluster/project/shared-runtime",
       launch=launch,
       pre_launch=uploaded,
       transport=transport,
   )

Reconnect using only the transport profile, cluster storage path, and saved run ID:

.. code-block:: python

   from bioimageflow import RemoteWorkflowRun

   run = RemoteWorkflowRun.open(
       transport,
       "/cluster/project/my-workflow/results",
       saved_run_id,
   )

``progress()``, ``diagnostics()``, ``logs()``, ``wait()``, and ``cancel()`` are bounded public operations.
``result(destination=...)`` downloads to a private sibling, verifies the immutable manifest and every digest, and atomically installs the destination.
Record-owned and return-owned assets become local; declared external cluster paths remain cluster ``Path`` values.

Retry and recompute remotely
----------------------------

Use the same retained-run API as submitted-local execution:

.. code-block:: python

   prepared = run.prepare_retry()
   retry = prepared.submit()

For selected recomputation, pass ``RecomputeRequest(("nested/node",), cascade=True)`` and show ``prepared.plan.invalidations`` before confirmation.
Preparation and submission execute as public bounded cluster operations.
The cluster clones the retained workflow, copies run-owned input and bootstrap trees, reuses retained content-addressed uploads, checks active executions and revisions, applies the journaled cache-pointer invalidation, and creates a new run with ``parent_id``.
No laptop input is reread and the caller never accesses remote storage directly.

If transport or scheduler submission becomes uncertain, retain ``prepared.plan.retry_run_id`` and reconnect to that exact run.
Do not automatically prepare or submit another retry.

PSI/J submission integrity
--------------------------

BioImageFlow writes an immutable submit intent before calling PSI/J and an immutable receipt immediately after PSI/J returns a native scheduler ID.
Reconnect attaches to the exact persisted job identity.

If scheduler submission may have succeeded but no receipt was installed, the run remains ``prepared``, raises :class:`~bioimageflow.PSIJSubmissionUncertainError`, records the uncertainty, and is never submitted again automatically.
A queued scheduler job remains ``prepared`` until the orchestrator claims the run.

Retention
---------

Operators may remove abandoned partial transfers, expired prepared result bundles, and upload objects no longer referenced by retained runs.
Authoritative launcher state remains below the workflow storage root, while the transport staging root remains disposable according to these reference rules.
See :doc:`/reference/output_cache_storage` for the full directory and retention contract.
