# Unified workflow contract

This document is the normative library-to-host contract for recursive BioImageFlow workflows.
The golden examples are `tests/fixtures/unified_workflow_graph.json` and `tests/fixtures/unified_workflow_archive.json`.

## Public Python API

```python
Workflow(
    storage_path: str | Path,
    *,
    name: str = "workflow",
    display_name: str | None = None,
    engine: Literal["direct", "wetlands", "parsl"] = "wetlands",
    execution: Literal["parallel", "sequential"] = "parallel",
    on_progress: Callable[[ProgressEvent], None] | None = None,
    wetlands_config: dict[str, Any] | None = None,
    max_workers: int = 1,
    output_view: OutputView | Mapping[str, Any] | str | None = None,
)
```

`display_name` defaults to `name`.
Definition and node names must be non-empty and cannot contain `/`.
The slash is reserved as the scoped-path separator.

```python
Workflow.input(
    name: str,
    annotation: Any = None,
    *,
    kind: Literal["field", "dataframe"] = "field",
    default: Any = MISSING,
    id: str | None = None,
) -> WorkflowInputRef

Workflow.expose_input(
    node: Node,
    target: str | int,
    *,
    name: str,
    annotation: Any = None,
    kind: Literal["field", "dataframe"] = "field",
    default: Any = MISSING,
    id: str | None = None,
) -> WorkflowInputRef

Workflow.output(name: str, source: ColumnRef, *, id: str | None = None) -> None
Workflow.__call__(*, name: str | None = None, **bindings: Any) -> WorkflowNode
Workflow.compute(
    *targets: Node,
    inputs: Mapping[str, Any] | None = None,
    engine: DefaultEngine | SequentialEngine | ParslEngine | None = None,
    run_context: WorkflowExecutionContext | None = None,
    ...
) -> Any
Workflow.compute_steps(
    *targets: Node,
    inputs: Mapping[str, Any] | None = None,
    engine: DefaultEngine | SequentialEngine | ParslEngine | None = None,
    run_context: WorkflowExecutionContext | None = None,
    ...
) -> Iterator[NodeStep]
Workflow.from_python(
    path_or_module: str | Path | ModuleType,
    *,
    storage_path: str | Path,
) -> Workflow
Workflow.from_dict(data: dict[str, Any], *, storage_path: str | Path, ...) -> Workflow
Workflow.to_dict(*, include_custom_tools: bool = False) -> dict[str, Any]
Workflow.load(path: str | Path, *, storage_path: str | Path) -> Workflow
Workflow.import_archive(
    path: str | Path,
    destination: str | Path,
    *,
    storage_path: str | Path,
) -> Workflow
Workflow.export(path: str | Path) -> None
```

`storage_path` is required runtime state and never enters the recursive graph or portable archive.
Hosts choose it when constructing, loading, importing, or editing a workflow.
`WorkflowNode` is exported from `bioimageflow` and is the only public composite node type.
`WorkflowNode.workflow` is the captured, editable definition for that invocation.
`WorkflowNode[output_name]` returns a `ColumnRef` whose `column` is the stable output-port ID.

Calling a workflow requires an active, distinct parent workflow.
Root callers use `compute(inputs={...})`.
Invocation and root input mappings are keyed by interface names, while serialized bindings and workflow edges use stable IDs.
The input name `name` is reserved for assigning the structural invocation name.

Each call takes an independent structural snapshot.
The snapshot contains graph structure, interface definitions, defaults, constants, templates, enabled state, definition metadata, and tool-class references.
It does not copy callbacks, cancellation state, execution engines, environment managers, run views, or validation caches.

## Binding rules

A `field` input can target named processing-tool fields or `field` inputs on child workflow nodes.
A `dataframe` input can target positional `DataFrameTool` inputs or `dataframe` inputs on child workflow nodes.
A symbolic reference can fan out to multiple compatible targets.
The library rejects references used outside their owning active workflow, kind mismatches, a target already carrying an internal data edge, duplicate targets owned by different inputs, missing required values, and unknown invocation keys.

At root execution, field inputs receive ordinary values and DataFrame inputs receive complete DataFrames.
At nested invocation, field inputs receive constants or `ColumnRef` values and DataFrame inputs receive upstream nodes as complete results.
During parent construction, either kind can receive a compatible symbolic input from the parent.

Resolution precedence is explicit invocation or root value, interface default, local constant or tool default, then `missing_input`.
Invocation bindings never mutate interface defaults.

## Recursive graph grammar

Every graph has exactly these top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1`. |
| `name` | string | Stable definition identity. |
| `display_name` | string | Editable presentation metadata. |
| `interface` | object | Exactly `inputs` and `outputs`. |
| `nodes` | array | Immediate tool and workflow nodes. |
| `edges` | array | Immediate column and DataFrame edges. |
| `config` | object | Root-capable definition metadata. |

Unknown fields and unversioned graphs are rejected.

An input record contains `id`, `name`, `kind`, optional `schema`, optional serialized `default`, and `targets`.
Target records have `node` and `port`.
A tool field port is `{"kind": "field", "name": <field>}`.
A positional port is `{"kind": "positional", "index": <integer>}`.
A child workflow port is `{"kind": "workflow", "id": <stable-input-id>}`.

An output record contains `id`, `name`, optional `schema`, and `source`.
`source` contains `node` and `column`.
For a tool source, `column` is the tool output column name.
For a workflow-node source, `column` is its stable output-port ID.

A tool node has:

```json
{
  "name": "generate",
  "type": "tool",
  "tool_module": "bioimageflow_common_tools.generate",
  "tool_class": "Generate",
  "tool_package": null,
  "tool_package_version": null,
  "constants": {},
  "output_templates": {},
  "enabled": true,
  "source_module": "m_optional"
}
```

`output_templates`, `enabled`, and `source_module` are omitted when they carry their defaults or do not apply.
Constants use `serialize_constant` envelopes.

A workflow node has:

```json
{
  "name": "child",
  "type": "workflow",
  "workflow": {"schema_version": 1},
  "bindings": {
    "input-diameter": {"__type__": "float", "value": 25.0}
  },
  "enabled": false
}
```

`workflow` is a complete recursive graph.
`bindings` contains constants only and is keyed by stable child-input IDs.
`enabled` is omitted when true.
An incoming edge and constant binding for the same input are mutually exclusive.

A column edge has exactly:

```json
{
  "type": "column",
  "id": "edge-image",
  "source_node": "files",
  "source_output": "path",
  "target_node": "child",
  "target_input": "input-image"
}
```

A DataFrame edge has `type`, `id`, `source_node`, and `target_node`, plus exactly one of `target_position` for a tool or `target_input` for a child workflow port.
Edge IDs are unique and stable.
Unknown variants and malformed endpoint combinations are rejected.

`config` accepts `engine`, `execution`, and `output_view`.
Nested configuration is retained as definition metadata, but root execution engine, cancellation, progress, output-view, and environment-manager context take precedence.
Storage is supplied separately at materialization time and inherited by nested workflows.
The serialized `engine` value is only a preference string and accepts `direct`, `wetlands`, or `parsl`.
Live engine objects, Parsl Config/DFK objects, executor bindings, routes, task policy, credentials, and `WorkflowExecutionContext` never enter graph or archive data.
An explicit engine passed to a root compute call has precedence over the stored preference.

## Portable archive envelope

The portable archive and recursive graph are separate contracts:

```json
{
  "archive_version": 1,
  "workflow": {"schema_version": 1},
  "custom_sources": []
}
```

The envelope has exactly these three fields.
`custom_sources` is deduplicated across the complete recursive graph.
Each record has an explicit `id`, canonical `module`, `filename`, content hash, and either one source string or a hashed `files` bundle.
Tool nodes refer to records through `source_module`.
Source identity is the explicit ID plus verified content; class name alone is never an identity.
Two source IDs can therefore export the same class name without shadowing one another.

`to_dict()` returns the graph.
`to_dict(include_custom_tools=True)` and JSON `export()` return the envelope when custom sources exist.
ZIP export stores the same envelope as `workflow.json`.
`from_dict()` and `load()` preserve source references recursively.

## Trusted Python materialization

Every shipped or documented reusable workflow module exports exactly `build_workflow`.
It is callable without required arguments, creates a fresh `Workflow`, builds deterministically without executing tools, represents runtime values with interface inputs, uses explicit stable IDs, and publishes meaningful outputs.

`from_python()` executes only that exact symbol and calls it once.
It rejects absent, non-callable, tuple-returning, and non-`Workflow` factories.
Installed-module imports use normal Python import semantics.
File imports first capture the entry directory's Python source bytes, copy that snapshot to a fresh import context, purge stale local modules for the materialization, execute the entry, and capture recursive custom sources before releasing the context.
Export uses the resulting object and never reruns the factory.

## Compilation and execution identifiers

Stored node names never include `/`.
Compilation recursively expands workflow nodes and assigns scoped executable paths such as `outer/inner/tool`.
It assigns every real tool a stable ordinal from deterministic topological order, using scoped path as the ready-node tie breaker.
Progress events, run-view node keys, `compute_steps()` steps, cache entries, validation paths, and internal plan entries use these scoped paths.
Workflow-node aggregate plan entries use the workflow node's own scoped path and do not own cache records.

Every enabled internal terminal is a completion dependency.
Published providers supply boundary values and downstream data signatures.
Detached completion dependencies must run or hit cache before the boundary succeeds, but their signatures do not invalidate consumers of unchanged published outputs.
Failure or cancellation of any completion dependency fails or cancels the boundary, blocks its downstream consumers, and preserves the failing scoped path.

Published output Series must have compatible indexes.
The boundary DataFrame labels columns with current output names.
Renaming an output therefore preserves stable-ID column connectivity but changes whole-DataFrame and root result labels.
A workflow with no outputs returns a canonical zero-row, zero-column DataFrame after all enabled terminal branches complete.

Disabling a workflow node disables its complete subtree.
`compute_steps()` yields only real tool steps, including disabled ordinary tools as skipped steps; it never yields workflow-node aggregate steps, and a disabled workflow node is not expanded.
`plan()` reports executable tool entries and one aggregate entry per workflow node.
The aggregate is `SKIPPED` when disabled, `CACHED` only when every executable internal is cached, `PENDING_UPSTREAM` when an internal final selection depends on unknown upstream records, and otherwise `UNEXECUTED`.
`PENDING_UPSTREAM` retains a provider/selector provenance recipe and never fabricates a reusable result key.

One root call owns one `WorkflowExecutionContext`, and the exact object propagates through every synthetic root wrapper and recursive boundary.
A `Workflow` permits one active public execution.
When sibling work fails, the scheduler drains submitted work and selects the primary error by `(compiled node ordinal, first input position, task ID)`, independently of completion order.

Cache invalidation and clearing accept scoped tool paths and workflow-node paths.
A workflow-node path selects its complete subtree; cascading crosses workflow boundaries according to data dependencies.

## Validation errors

`Workflow.validate()` recursively checks definition and node names, interface ID/name uniqueness, target/source existence, schema compatibility, missing and extra bindings, binding kinds, cycles, recursive containment, constants, environments, and output mappings.
A standalone definition may have unsupplied required interface inputs.
Root `compute(inputs=...)` validates values, while nested invocation requires every child input to have an edge, constant, parent symbolic input, interface default, or local fallback.

Nested errors use `ValidationError.path` from root workflow node to leaf scope and keep the leaf `node`, `field`, `edge`, and `edge_id` identifiers.
`from_dict(validate_only=True, partial=True)` may return an incomplete editor graph and structured errors, but never normalizes another schema.

## Deliberately unsupported operations

- No second workflow-definition class, workflow registry, or workflow decorator.
- No alternate factory symbol or module-level shared workflow convention.
- No portable dependency on Python factories after materialization.
- No schema migration, compatibility flag, deprecated alias, or unversioned dictionary loader.
- No unknown node/edge/config variants or best-effort endpoint guessing.
