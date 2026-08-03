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

.. autofunction:: validate_remote_execution_profile

.. autoclass:: RemoteProfileValidationReport
   :members:

.. autoclass:: RemoteProfileDiagnostic
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

Submitted execution
-------------------

.. autoclass:: ParslConfigRef
   :members:

.. autoclass:: OrchestratorLaunchConfig
   :members:

.. autoclass:: PSIJLaunchConfig
   :members:

.. autoclass:: PreLaunchScript
   :members:

.. autofunction:: submit_workflow

.. autoclass:: WorkflowRun
   :members:

Remote transport and immutable preparation
------------------------------------------

.. autoclass:: SSHSubmissionTransport
   :members:

.. autoclass:: LocalUpload
   :members:

.. autofunction:: inspect_remote_node_paths

.. autoclass:: RemoteNodePathPlan
   :members:

.. autoclass:: RemoteNodePathInput
   :members:

.. autofunction:: prepare_remote_submission

.. autoclass:: PreparedRemoteSubmission
   :members:

.. autoclass:: PreparedSubmissionManifest
   :members:

.. autoclass:: PreparedSubmissionEntry
   :members:

.. autoclass:: PreparedSubmissionExternalSource
   :members:

.. autoclass:: RemoteWorkflowRun
   :members:

Failures
--------

.. autoclass:: NodeFailureDiagnostic
   :members:

.. autoclass:: ParslTaskError
   :members:

.. autoclass:: BackendNotSupportedError

.. autoclass:: PSIJSubmissionUncertainError

.. autoclass:: WorkflowRunFailedError

.. autoclass:: WorkflowRunLostError

.. autoclass:: WorkflowRunNotReadyError

.. autoclass:: WorkflowRunResultUnavailableError

.. autoclass:: LauncherError

.. autoclass:: LauncherProtocolError

.. autoclass:: LauncherStateConflictError
