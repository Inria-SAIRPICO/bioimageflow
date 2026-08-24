Execution API
=============

This page groups the public types and operations used by execution integrations.
The preceding pages explain when and how to use them.

Capabilities and validation
---------------------------

.. currentmodule:: bioimageflow

.. autofunction:: get_execution_capabilities

.. autoclass:: ExecutionCapabilityReport
   :members:

.. autoclass:: CapabilityStatus
   :members:

.. autofunction:: validate_parsl_config_ref

.. autoclass:: ParslConfigValidationReport
   :members:

.. autoclass:: IntegrationDiagnostic
   :members:

Planning and resources
----------------------

.. autofunction:: plan_distributed_execution

.. autoclass:: DistributedExecutionPlan
   :members:

.. autoclass:: DistributedNodePlan
   :members:

.. autoclass:: NodeResourceOverrides
   :members:

Attached Parsl
--------------

.. autoclass:: ParslEngine
   :members:

.. autoclass:: ParslTaskPolicy
   :members:

.. autoclass:: WorkerSlotCapacity
   :members:

.. autoclass:: ExecutorCapabilities
   :members:

.. autoclass:: WorkerEnvironmentAttestation
   :members:

.. autoclass:: ExecutorBinding
   :members:

Attached execution context and results
--------------------------------------

.. autoclass:: WorkflowExecutionContext
   :members:
   :no-index:

.. autoclass:: ExecutionProviderOutcome
   :members:
   :no-index:

Submitted local execution
-------------------------

These advanced values launch a reconnectable orchestrator on the same machine or through an application-owned process service.
Managed SSH cluster execution uses :class:`bioimageflow.cluster.RemoteCluster` instead.

.. autoclass:: ParslConfigRef
   :members:

.. autoclass:: OrchestratorLaunchConfig
   :members:

.. autofunction:: submit_workflow

.. autoclass:: WorkflowRun
   :members:

.. autoclass:: RunRetryPlan
   :members:

.. autoclass:: RecomputeRequest
   :members:

.. autoclass:: RetryInvalidation
   :members:

Managed remote clusters
-----------------------

The canonical managed-cluster API is the :mod:`bioimageflow.cluster` module.

.. currentmodule:: bioimageflow.cluster

.. autoclass:: RemoteCluster
   :members:

.. autoclass:: ClusterEnvironment
   :members:

.. autoclass:: SetupScript
   :members:

.. autoclass:: SchedulerJob
   :members:

.. autoclass:: ParslConfiguration
   :members:

.. autoclass:: LocalUpload
   :members:

.. autoclass:: ClusterDeployment
   :members:

.. autoclass:: PreparedClusterInvocation
   :members:

.. autoclass:: PreparedInvocationManifest
   :members:

.. autoclass:: PreparedInvocationEntry
   :members:

.. autoclass:: ClusterConnectionReport
   :members:

.. autoclass:: ClusterValidationReport
   :members:

.. autoclass:: RemoteExecutionPlan
   :members:

.. autoclass:: RemoteNodePlan
   :members:

.. autoclass:: RemoteWorkflowRun
   :members:

.. autoclass:: ClusterCleanupPlan
   :members:

.. autoclass:: ClusterCleanupCandidate
   :members:

.. autoclass:: ClusterCleanupReport
   :members:

.. autoclass:: ClusterDiagnostic
   :members:

.. autoclass:: ClusterOperationError
   :members:

Managed Parsl factories
-----------------------

.. currentmodule:: bioimageflow.parsl

.. autoclass:: WorkerSlot
   :members:

.. autoclass:: ParslFactoryResult
   :members:

.. autoclass:: ParslFactoryRuntime
   :members:

Remote input inspection
-----------------------

.. currentmodule:: bioimageflow

.. autofunction:: inspect_remote_node_paths

.. autoclass:: RemoteNodePathPlan
   :members:

.. autoclass:: RemoteNodePathInput
   :members:

Failures
--------

.. currentmodule:: bioimageflow

.. autoclass:: NodeFailureDiagnostic
   :members:

.. autoclass:: ParslTaskError
   :members:

.. autoclass:: BackendNotSupportedError

.. autoclass:: WorkflowRunFailedError

.. autoclass:: WorkflowRunLostError

.. autoclass:: WorkflowRunNotReadyError

.. autoclass:: WorkflowRunResultUnavailableError

.. autoclass:: WorkflowRunRetryError

.. autoclass:: WorkflowResultDestinationError

.. autoclass:: WorkflowResultExportError

.. autoclass:: WorkflowResultIntegrityError

.. autoclass:: LauncherError

.. autoclass:: LauncherProtocolError

.. autoclass:: LauncherStateConflictError
