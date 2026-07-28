# Iterative Review Log

## Baseline

- Item: `docs/parsl_portability_implementation_plan.md`, to be replaced by `docs/parsl_cluster_execution_implementation_plan.md`.
- Goal: define four focused work packages that let a laptop submit a workflow over SSH to an HPC cluster, use PSI/J on the cluster to launch one orchestrator scheduler job, let the existing Parsl engine allocate workers, follow progress and logs, reconnect or cancel, and retrieve the final result.
- Inputs: `docs/parsl_distributed_engine_specs.md`, the normative platform documentation, the completed Phase 1a and Phase 1b implementation, and the current library and tests.
- Constraints: no S3 or general remote run store; preserve the cluster-side cache and output layout; assume the orchestrator and workers share the cluster filesystem; keep `DataFrameTool` on the orchestrator; use PSI/J for supported scheduler abstraction; use SSH/SFTP for the laptop-cluster boundary; exclude streaming and general no-shared-filesystem worker staging; describe only the intended final architecture, with no compatibility layer.
- Started: 2026-07-28.
- Max iterations: 5.
- Convergence rule: stop when changes are no longer meaningful or remaining work is too broad or detailed for the requested scope.

## Iteration 1

- Reviewer: `psij_plan_draft`.
- Meaningful: yes.
- Changes:
  - Replaced the 1,042-line broad portability plan with a 500-line cluster-execution plan.
  - Limited delivery to exactly four work packages covering PSI/J launch, SSH submission, remote run control/result retrieval, and final integration/documentation.
  - Froze a compact public API with commented submission, waiting, reconnection, cancellation, and result-download examples.
  - Mapped completed Phase 1a and Phase 1b behavior into the remaining implementation rather than respecifying it.
  - Added PSI/J receipt/attachment, SSH/SFTP protocol and safety, atomic transfer, remote lifecycle, testing, checkpoints, acceptance, and GUI-handoff requirements.
  - Preserved the cluster storage/cache/output layout and excluded storage overrides and heuristic path rewriting.
- Rationale:
  - The replaced plan solved a much broader disconnected-storage and worker-staging problem than the user's laptop-to-HPC goal requires.
  - The new plan uses PSI/J only where scheduler commands are available and uses SSH for the actual laptop-cluster boundary.
  - Reusing the implemented engine and launcher lifecycle keeps the remaining work focused and testable.
- Deferred:
  - S3 and object stores, remote run-store abstraction, durable URI expansion, generic worker staging, remote or partitioned `DataFrameTool`, streaming, native scheduler adapters including OAR, compatibility code, and GUI implementation.

## Iteration 2

- Reviewer: `psij_plan_review_2`.
- Meaningful: yes.
- Changes:
  - Bound each request digest durably to a preallocated UUID4 run ID before launcher allocation.
  - Defined fail-closed behavior for an uncertain PSI/J submission without terminalizing a job that may have been accepted.
  - Corrected reconnection to use asynchronous PSI/J attachment followed by bounded observation past `NEW`.
  - Preserved uploaded root basenames in content identity so `Path.name` and `Path.stem` remain stable.
  - Required root DataFrame path cells to be absolute cluster paths and rejected embedded uploads or relative paths.
  - Made log pagination byte-accurate through base64 chunks decoded only after reassembly.
  - Delegated cancellation, claim expiry, and lost-run recovery explicitly to the existing Phase 1b implementation.
  - Clarified PSI/J executor prerequisites and expanded repository validation gates.
- Rationale:
  - These changes close realistic scheduler-receipt, reconnection, path-semantics, and multibyte-log correctness gaps without broadening the architecture.
  - They prevent the plan from rebuilding Phase 1b lifecycle behavior or claiming guarantees PSI/J cannot provide.
- Deferred:
  - Scheduler-specific lookup by submission token, uploads embedded in DataFrame cells, generalized staging retention or run stores, native scheduler adapters, and GUI implementation.

## Iteration 3

- Reviewer: `psij_plan_review_3`.
- Meaningful: yes.
- Changes:
  - Separated operation request IDs from server-issued upload IDs to remove retry and receipt ambiguity.
  - Defined one atomically committed bundle for the workflow payload, root DataFrames, and every explicit upload.
  - Required staging and canonical storage to be disjoint, staging to be worker-visible, and uploads to be retained while referenced.
  - Removed a WP2/WP3 dependency cycle by keeping transport internal in WP2 and wiring the public `RemoteWorkflowRun` in WP3.
  - Required authoritative launcher-status rereads after PSI/J reconciliation and constrained polling mutations.
  - Preserved and tested laptop-local `SharedArray` rehydration.
  - Strengthened real-site acceptance by requiring a Parsl worker to consume an explicitly uploaded local input.
- Rationale:
  - These changes make retries, atomic submission, path ownership, package ordering, and remote result reconstruction implementable without a hidden general run-store layer.
  - The stronger smoke test demonstrates the complete laptop-upload to Parsl-worker path rather than only scheduler launch.
- Deferred:
  - Scheduler-token lookup, generalized staging garbage collection or run stores, native scheduler adapters, broader portability layers, and GUI implementation.

## Iteration 4

- Reviewer: `psij_plan_review_4`.
- Meaningful: yes.
- Changes:
  - Required an explicit positive orchestrator walltime instead of accepting PSI/J's implicit default.
  - Pinned PSI/J's executor work directory to durable shared launcher control storage so its submission and exit metadata remain available for cross-process attachment.
  - Made content-addressed uploads read-only and required manifest revalidation before reuse.
- Rationale:
  - Explicit walltime prevents unexpectedly short orchestrator allocations.
  - Durable PSI/J auxiliary metadata is necessary for reliable attachment and recovery from a different cluster-side command process.
  - Read-only verified uploads protect shared content identity from worker mutation.
- Deferred:
  - Scheduler-token lookup, generalized staging garbage collection or run stores, native scheduler adapters, broader portability layers, and GUI implementation.

## Iteration 5

- Reviewer: `psij_plan_review_5`.
- Meaningful: yes.
- Changes:
  - Routed PSI/J reconciliation and cancellation through cluster-local `WorkflowRun.refresh()` and `WorkflowRun.cancel()`.
  - Froze the scheduler queue mapping to `JobAttributes.queue_name` and the project mapping to `JobAttributes.account`.
  - Added focused tests and acceptance wording for the shared lifecycle and PSI/J attribute mappings.
- Rationale:
  - Direct cluster-side and SSH-transported runs must use one launcher lifecycle rather than separate reconciliation or cancellation behavior.
  - Explicit PSI/J field mappings remove scheduler-configuration ambiguity before implementation.
- Deferred:
  - Scheduler-token lookup, generalized staging garbage collection or run stores, native scheduler adapters, broader portability layers, and GUI implementation.

## Convergence

- Stopped after: 5 iterations.
- Reason: the maximum iteration cap was reached after the final pass made a bounded lifecycle and PSI/J configuration correction.
- Final item: `docs/parsl_cluster_execution_implementation_plan.md`.
- Residual risk: the review did not reach a no-change pass before the cap; the plan therefore requires its documented work-package contract checkpoints and focused tests during implementation.
