bioimageflow_core
=================

The worker-safe core package. Installed in all environments (main process and
workers). It declares NumPy for shared-memory array helpers and avoids
orchestrator-only dependencies such as pandas and pydantic.
Core supports scientific worker Python >=3.9; the orchestrator floor is >=3.10.
The declaration and current typed-value contracts are defined in :doc:`/specs`; generated member documentation is navigation, not a claim that every internal helper is supported.
Public exported symbols and explicitly documented APIs form the curated support boundary.
The source package declares ``py.typed`` for PEP 561 type discovery; the packaging tier checks its inclusion in actual wheels and source distributions.

Types
-----

.. automodule:: bioimageflow_core.types
   :members:
   :undoc-members:
   :show-inheritance:

Environment
-----------

``EnvironmentSpec(name, dependencies, allow_flexible_versions=False)`` captures an independently validated recipe in a frozen object.
Each ``dependencies`` read returns a fresh ordinary nested dictionary/list projection; caller or returned-recipe edits do not mutate that captured authority.
Pip validation parses complete PEP 508 requirements and preserves their text; Conda validation distinguishes exact version/build constraints from fuzzy versions and wildcards.
See :doc:`/specs` §3.1 for the exact and flexible admission rules.

.. automodule:: bioimageflow_core.environment
   :members:
   :undoc-members:
   :show-inheritance:

Current processing contracts
----------------------------

The public logical DTOs are ``ProcessingTask``, ``RowInvocation``, ``ConsumedRow``, ``OutputGroup`` and ``ProcessingTaskResult``.
Task/result v4 codecs preserve one current typed grammar and exact correlation, including row consumption, declaration attestation and the ordered consumed-row identities;
decode is pure and cannot attach, allocate or register owners.
Physical ``mode`` chooses row-chunk or batch execution independently of mapped or collective input meaning.
Mapped results have one singleton-consumption group per input and retain zero/one/many output expansion.
Collective results have exactly one group containing every consumed input identity; an empty batch has an empty consumed tuple and may still produce outputs.
Batch constants/output paths are available as ``ExecutionContext.batch_arguments``; genuine auxiliary selected records use ``reference_rows`` and never become synthetic observation rows.
``ReferenceRow.arguments`` is a dictionary of current typed values.
Portable IOModel annotations are distinct from runtime numeric arrays/scalars, Paths, bytes, literal dictionaries and scoped references.
See :doc:`/specs` §5.2 for the grammar and refusal rules; no historical DTO aliases or wire fallbacks are required.

``encode_processing_value(value, encode_leaf=...)`` and ``decode_processing_value(value, decode_leaf=...)`` share the exact dictionary/list/tuple traversal with current task/result codecs.
Each optional callback receives a leaf and the keyword ``is_key``; it owns the explicit leaf representation and its metadata validation, while Core validates live leaves before encoding and reconstructed leaves after decoding.
Keys remain exactly None, bool, int, float, str or bytes; container nodes are refused in key positions before callbacks, and a decoded leaf cannot introduce a container or an unsupported object.
Custom asset decoders must refuse key roles before hydration and validate record ownership rather than interpret task grant references as persisted assets.
The encoding callback sees native arrays without a preliminary copy and SharedArray references with their bound owner intact.
Default callbacks retain Task4 wire values and pure decoding without attachment; explicit custom callbacks own any leaf I/O they perform.

``describe_tool_declaration(tool)`` captures the actual concrete Inputs/Outputs semantic declaration through the shared finite annotation grammar.
``describe_io_model(model, passthrough=False)`` projects ordered fields without serializing defaults or GUI values; ``None`` remains an explicit absent declaration for controller-only tools.
``validate_tool_declaration(value)`` admits a detached concrete processing declaration, ``declaration_digest(value)`` hashes its strictly typed facts, and ``compare_tool_declarations(expected, actual)`` refuses with a field path on mismatch.
The worker compares after tool construction and before the scientific method; its successful result digest must match the task before output acceptance.
Different Python IOModel class names do not change semantic equality, while boolean/integer/float literal distinctions and field order remain authoritative.
``DECLARATION_CONTRACT_VERSION`` distinguishes attested cache records from records produced before this contract.
These APIs do not attest arbitrary initializers, custom validators, later process-global mutation or installed dependency content.

Selected primary content
------------------------

``WorkerToolOrigin`` uses the one current ``bioimageflow.worker_tool_origin.v2`` grammar with mandatory ``PrimaryContentProof``.
The public variants are ``InstalledModuleOrigin``, ``VersionedModuleOrigin``, ``SharedModuleOrigin``, ``SourceFileOrigin`` and ``ArchiveModuleOrigin``; earlier aliases and wire fallbacks are absent.
``PrimaryFileMember`` records an admitted module, normalized file locator and source digest.
``PrimaryInstalledMember`` records the selected distribution/version and its contained member identity; actual local PEP 610 editable membership is admitted through its metadata-owned source layout, without inventing RECORD entries.
``PrimaryCallback`` records the module and qualified owner of each represented constructor or scientific callback.
``PrimaryBuiltinCallback`` explicitly represents supported immutable ``object.__new__`` and ``object.__init__`` owners.
The four callback roles are ordered ``__new__``, ``__init__``, ``process_row`` and ``process_batch``; inherited row and overridden batch owners remain independent authorities.

``capture_primary_content(tool_class, distribution=None, package_root=None)`` returns an admission retaining the proof and captured source bytes.
Its ``source_hash(path_or_module)`` reads the held snapshot, ``scientific_facts()`` projects portable content identity independently of operational locators, and ``qualification`` records unresolved initializer or dependency facts.
``attest(tool)`` compares represented resident owners and supported callable/literal facts against those bytes; ``validate_primary_content(tool, proof)`` admits the proof's current members and performs that comparison.
``bioimageflow_core.primary_content.encode_primary_content`` and ``decode_primary_content`` enforce the strict proof grammar; decoding performs no source read or import.
The controller admits primary content before cache lookup.
The worker captures matched bytes before import, checks resident class and callback owners before construction, and checks the instance after construction and on every reuse before science.
Fresh imports compile held bytes even when older timestamp-valid bytecode exists; interpreter-local compiled comparison is never a cross-Python bytecode wire token.
``execute_processing_task`` retains one admission and its held-byte import finder through row or batch callbacks and result handling, including first-time represented helper imports.
Finder scopes serialize within one process; separate WorkerPool processes remain parallel.
``load_worker_tool`` covers admission, construction and reuse; its import scope ends when the instance is returned.

A conflicting canonical Shared root refuses while preserving the prior namespace and proven instance.
Distinct Source content namespaces and versioned or archive content-scoped namespaces retain their coexistence contract.
These supported facts neither sandbox initializer/constructor side effects nor attest arbitrary class state, process-global state, unrepresented dependencies or native relocation.
See :doc:`/specs` §§3.10, 5.2 and 6 for selection, scientific identity and refusal boundaries.

Versioned import authority
--------------------------

``bioimageflow_core.import_context.admit_import_root(root, import_package=..., dependency_authority=...)`` captures declared import selection facts without importing tool code.
``selected_import_root(admission)`` serializes owned import contexts and restores caller search paths on success or error.
``selected_installation`` admits loaded distribution membership and exact dependency versions when present in the selected installation; host-only active requirements use compatible metadata-owned providers.
``managed_runtime`` uses compatible actual worker dependencies while keeping the selected tool under its synthetic package path.
Unknown ownership or incompatible active declarations refuse without purging foreign modules.
``ImportRootAdmission.to_scientific_facts()`` returns detached declared dependency versions, while ``observed_dependencies`` reports current executor authority without claiming byte equality.
Inactive package-wide dependencies do not force orchestrator packages into Core-only scientific workers.
Successful-return validation covers newly imported dependencies; arbitrary external threads, initializer side effects, dependency contents and complete solver-generation attestation remain outside this finite contract.

Supported external-command helpers
-----------------------------------

The supported helper contract owns explicit argv/cwd, staged output, timeout and error boundaries without global chdir or silent partial-output success.
Timeout/interruption cannot certify physical termination without the owning drain fence.
Exact public helper signatures and conformance remain mapped by the ordered test/code review; this reference does not invent an additional method.

Viewer Requirements
-------------------

Python ``NapariRequirement`` constructors accept package strings and requirement objects as authoring shorthand.
Portable package collections are lists of strict ``PackageRequirement`` dictionaries; constructor shorthand is not portable wire grammar.
Exported viewing manifests retain detached entries and return fresh ``outputs`` and ``to_dict()`` projections without changing held completeness.

.. automodule:: bioimageflow_core.viewer
   :members:
   :undoc-members:
   :show-inheritance:

Definition values
-----------------

``IOModel.capture_defaults()`` returns detached declared defaults with missing fields distinct from explicit None.
``bioimageflow_core.defaults.snapshot_value`` detaches supported semantic containers/numeric values without cloning scoped resource owners.
An ``EnvironmentSpec`` snapshot reconstructs an equal independent specification from its captured name, recipe and flexibility flag.
Constructor-supplied values retain caller identity; omitted mutable defaults do not share declaration storage.

Tool Base Classes
-----------------

.. automodule:: bioimageflow_core.tool
   :members:
   :undoc-members:
   :show-inheritance:

Arguments
---------

.. automodule:: bioimageflow_core.arguments
   :members:
   :undoc-members:
   :show-inheritance:

I/O
---

.. automodule:: bioimageflow_core.io
   :members:
   :undoc-members:
   :show-inheritance:

Shared Memory
-------------

.. automodule:: bioimageflow_core.shm
   :members:
   :undoc-members:
   :show-inheritance:

Shared-array ownership
----------------------

.. automodule:: bioimageflow_core.shared_memory
   :members: SharedMemoryContext, SharedArrayLease, CleanupStatus, WorkerGrant, collect_input_scopes, validate_scope_descriptor
