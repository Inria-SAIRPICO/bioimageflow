# Platform Integration Guide — Source-Only Tools and Dynamic Output Schemas

This document tells a platform agent (working in
`/Users/amasson/Travail/bioimageflow-platform`) how to consume the wire-format
additions shipped in commit `da5ad87` of the bioimageflow library
(plan: `plan-dataframetool-source-and-dynamic-outputs.md`).

The companion plan on the platform side is
`bioimageflow-platform/plan-canvas-pin-redesign.md` — read that for the GUI
decisions; this doc is the **library-side contract** it depends on.

The library bumps are purely additive — nothing the platform already calls
broke. The work below is *adoption* of new capabilities, not migration.

---

## TL;DR — what's new in the library wire format

Three new helpers and one new error kind, all imported from
`bioimageflow.validation` (or re-exported from `bioimageflow`):

| New surface | Purpose |
|---|---|
| `serialize_tool_metadata(tool_class) -> dict` | Per-tool flags: `tool_type`, `accepts_upstream`, `dynamic_outputs` |
| `serialize_resolved_outputs(node) -> dict` | Per-node resolved column schema, given current `kwargs` and upstream wiring |
| `Node.get_output_schema() -> dict \| None` | Same data as `serialize_resolved_outputs` but raw (no `{"resolved": …}` envelope) |
| `SourceToolUpstreamError` (`ValidationErrorKind = "source_tool_upstream"`) | Raised when source tools (`accepts_upstream=False`) get positional args |

Source-of-truth: `specs.md` §2.4 (wire format), §3.5 (DataFrameTool / dynamic
schema), §4.2 (ColumnRef construction-time validation), §6.6 (error reference).

---

## 1. Source-only DataFrameTools — `accepts_upstream`

### Library change

`DataFrameTool` now has a class attribute `accepts_upstream: bool = True`.
`Files` and `Generate` set it to `False`. Constructing a source tool with
positional args raises `SourceToolUpstreamError` (kind `source_tool_upstream`).

### What the platform must change

**Backend** — `tool_registry.py:120-189`, `_register_tool_from_class`:

The current code computes `tool_type` itself with `issubclass(...)`. Replace
that block with a single call to `serialize_tool_metadata` and surface the
new flags on `ToolMetadata`:

```python
# Before (lines 134-147):
try:
    from bioimageflow.dataframe_tool import DataFrameTool
    tool_type = "DataFrameTool" if issubclass(tool_cls, DataFrameTool) else ""
except ImportError:
    tool_type = ""
if not tool_type:
    from bioimageflow_core.tool import ProcessingTool
    if issubclass(tool_cls, ProcessingTool):
        tool_type = "ProcessingTool"
    else:
        tool_type = "BaseTool"

# After:
from bioimageflow.validation import serialize_tool_metadata
meta = serialize_tool_metadata(tool_cls)
tool_type = meta["tool_type"]            # "DataFrameTool" | "ProcessingTool"
accepts_upstream = meta["accepts_upstream"]
dynamic_outputs = meta["dynamic_outputs"]
```

`ToolMetadata` (`backend/src/bioimageflow_server/models/tools.py:52`) needs
two new fields:

```python
class ToolMetadata(BaseModel):
    ...
    tool_type: str
    accepts_upstream: bool = True       # NEW
    dynamic_outputs: bool = False       # NEW
    ...
```

These must be exposed on `GET /tools` and `GET /tools/{name}` so the
frontend can read them.

**Frontend** — Vue Flow node rendering (where the positional input pin is
drawn for DataFrameTool nodes):

- When `tool_type === "DataFrameTool"` **and** `accepts_upstream === false`,
  do not render the positional input pin / "1" handle. The current code
  unconditionally renders it for any DataFrameTool — that's the bug this
  flag fixes for `Files` and `Generate`.
- A user trying to drag a connection into a non-existent positional pin
  is now physically impossible (no pin) instead of silently ignored at
  runtime.

**Validation surface (optional)** — if the platform forwards a graph it
synthesized (e.g. via `Workflow.from_dict(..., partial=True)`) that
violates `accepts_upstream`, the library reports a `ValidationError` with
`kind="source_tool_upstream"`. The frontend already has a generic
`ValidationError` renderer; just make sure the kind is whitelisted.

### Validity check

```python
serialize_tool_metadata(Files)
# {'tool_type': 'DataFrameTool', 'accepts_upstream': False, 'dynamic_outputs': False}

serialize_tool_metadata(InnerJoin)
# {'tool_type': 'DataFrameTool', 'accepts_upstream': True, 'dynamic_outputs': True}

serialize_tool_metadata(StubSegmenter)  # any ProcessingTool
# {'tool_type': 'ProcessingTool', 'accepts_upstream': True, 'dynamic_outputs': False}
```

---

## 2. Dynamic output schemas — `serialize_resolved_outputs(node)`

### Library change

Two override points on `DataFrameTool`:

1. `resolve_outputs(cls, inputs)` — for tools whose output **column names**
   come from inputs. `Generate(column_name="sensitivity", ...)` returns
   `{"sensitivity": {...}}`.
2. `resolve_merge_schema(cls, upstream_schemas, inputs)` — for built-in
   merge tools (`InnerJoin`, `CrossJoin`, `JoinOnColumn`, `Concat`,
   `Collect`). Computes the merged column schema from upstream schemas
   plus this tool's `kwargs` (e.g. `suffixes`, `join_column`).

`Node.get_output_schema()` walks the graph and dispatches to whichever
applies. `serialize_resolved_outputs(node)` wraps it for the wire:

```json
{
  "resolved": true,
  "columns": {
    "path":        {"type": "Path", "default": null, "image_spec": null},
    "filename":    {"type": "str",  "default": null, "image_spec": null},
    "sensitivity": {"type": "any",  "default": null, "image_spec": null},
    "size":        {"type": "any",  "default": null, "image_spec": null}
  }
}
```

When the schema is unresolvable (e.g. an upstream merge has unconfigured
inputs), the response is `{"resolved": false, "columns": {}}` — render a
placeholder pin and re-call when more inputs are supplied.

The `"any"` type-string is reserved (specs.md §2.4) for columns whose
runtime type isn't known until execution. GUIs should treat it as
"compatible with any consumer" for connection-validity checks.

### What the platform must change

**Backend** — new endpoint `POST /workflows/{wf_id}/nodes/{node_name}/resolved_outputs`
(or whatever the existing per-node introspection convention is). The
handler:

```python
from bioimageflow.validation import serialize_resolved_outputs

@router.post("/workflows/{wf_id}/nodes/{node_name}/resolved_outputs")
def resolved_outputs(wf_id: str, node_name: str) -> dict:
    wf = workflow_registry.get(wf_id)        # however your registry works
    node = wf._nodes[node_name]
    return serialize_resolved_outputs(node)
```

The endpoint must be cheap (called every time a kwarg changes on a
DataFrameTool node). `Node.get_output_schema()` is pure / side-effect-free
but recursive over the upstream subgraph — for very large DAGs you may
want an in-memory memo keyed by node id + revision counter. For typical
GUI workflows (< 100 nodes) the call is microseconds.

**Frontend** — node card rendering:

- Read `dynamic_outputs` from the tool catalog (`ToolMetadata`).
- If `false` (most ProcessingTools, `Files`, `FilterRows`): render output
  pins from `ToolMetadata.outputs` once, keep them static.
- If `true` (`Generate`, all merge tools, custom tools that override
  `resolve_outputs`): on every kwarg change or upstream-edge change for
  this node, call the resolved-outputs endpoint and re-render the output
  pins from the response.
  - `resolved === false` → render a single placeholder "?" pin, no
    column drag-and-drop targets.
  - `resolved === true` → render one pin per `columns` entry, labelled
    with the column name. The existing `ColumnRef` edge representation
    on the wire (`{from, to, column, field, id}`) does not change.

### The motivating workflow

`example-workflows/parameter_space_exploration/workflow.py`:

```python
images = Files()(path="…")
sens   = Generate()(column_name="sensitivity", values=[0.1, 0.2, 0.3])
size   = Generate()(column_name="size",        values=[10, 20])
grid   = CrossJoin()(images, sens, size)
atlas  = Atlas()(
    input_image=grid["path"],
    p_value=grid["sensitivity"],
    gaussian_std=grid["size"],
)
```

Today the platform GUI cannot build this: `Generate` declares no static
`Outputs`, and `CrossJoin` declares no `Outputs` at all, so neither has
output pins to drag from. After the platform adopts this guide:

1. `Generate` gets one output pin (`sensitivity`, `size`, …) **as soon as
   the user types `column_name`**.
2. `CrossJoin` gets four pins (`path`, `filename`, `sensitivity`, `size`)
   the moment all three upstream nodes are wired and `Generate` nodes
   have `column_name` set.
3. The user drags `grid.sensitivity` into `Atlas.p_value` — the existing
   edge wire format works unchanged.

### The merge-tool kwargs that affect the resolved schema

Some merge tools have inputs that change column names. The platform must
include these kwargs in the node's wire-format `constants` block; the
library reads them off `Node._constant_bindings` when resolving:

| Tool | Kwarg | Effect on resolved schema |
|---|---|---|
| `CrossJoin` | `suffixes: tuple[str, str]` (default `("_left", "_right")`) | Suffixes overlapping columns from the first two upstreams |
| `JoinOnColumn` | `join_column: str` (required) | Required for resolution; without it `resolved=false` |
| `JoinOnColumn` | `suffixes: tuple[str, str]` | Same as CrossJoin |

If the user hasn't picked a `join_column` for a `JoinOnColumn`, the
endpoint will return `resolved=false`. Render the placeholder until they do.

---

## 3. Validity / acceptance tests on the platform side

The library ships a parity test set in
`tests/integration/test_gui_validation_api.py` (class
`TestSerializeResolvedOutputsWireFormat` and `TestSourceToolUpstream`).
Mirror these on the platform-backend side as integration tests against
the live endpoint:

- `Files`/`Generate` POST with positional upstream → backend forwards →
  library raises `SourceToolUpstreamError` → backend returns 400 with
  `kind="source_tool_upstream"`.
- `Generate` with no `column_name` → resolved-outputs endpoint returns
  `{"resolved": false, "columns": {}}`.
- `Generate` with `column_name="x"` → returns `{"resolved": true,
  "columns": {"x": {...}}}`.
- `CrossJoin(Files, Generate(column_name="sensitivity", ...),
  Generate(column_name="size", ...))` → returns four columns.

---

## 4. Quick-start checklist for the platform agent

In order:

1. ☐ Bump the `bioimageflow` and `bioimageflow-common-tools` workspace
   pins to a SHA at or after `da5ad87` (the commit landing this work).
2. ☐ `tool_registry.py` — replace the manual `issubclass` block with
   `serialize_tool_metadata(tool_cls)`; expose `accepts_upstream` and
   `dynamic_outputs` on `ToolMetadata`.
3. ☐ Add a per-node resolved-outputs endpoint that returns
   `serialize_resolved_outputs(node)`.
4. ☐ Frontend: hide positional input pin when `accepts_upstream === false`.
5. ☐ Frontend: when `dynamic_outputs === true`, replace static output pins
   with the result of the resolved-outputs endpoint, refreshing on kwarg
   or upstream-edge changes.
6. ☐ Whitelist `source_tool_upstream` in the `ValidationError` renderer.
7. ☐ Mirror the four parity tests above in the platform's integration suite.

When all seven boxes are checked, the GUI builds
`parameter_space_exploration` end-to-end without dropping into Python.

---

## 5. Reference — full surface

```python
from bioimageflow import (
    SourceToolUpstreamError,        # exception
    serialize_tool_metadata,        # (tool_class) -> {tool_type, accepts_upstream, dynamic_outputs}
    serialize_resolved_outputs,     # (node) -> {resolved: bool, columns: dict | {"_passthrough": True}}
    serialize_input_schema,         # (tool_class) -> per-input field schemas (unchanged)
    serialize_output_schema,        # (tool_class) -> per-output field schemas (unchanged, static-only)
)
from bioimageflow.dataframe_tool import DataFrameTool
DataFrameTool.accepts_upstream      # class attribute, default True
DataFrameTool.resolve_outputs       # classmethod, default returns serialize_output_schema(cls) or None
DataFrameTool.resolve_merge_schema  # classmethod, default returns None; overridden on built-in merge tools

from bioimageflow.node import Node
Node.get_output_schema              # ()-> resolved schema dict | None (raw, no envelope)
```

`ValidationErrorKind` literal now includes `"source_tool_upstream"`.
