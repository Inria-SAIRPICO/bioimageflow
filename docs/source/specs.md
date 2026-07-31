# BioImageFlow Library Specifications

## 1. Introduction and Scope

**BioImageFlow** is a Python library for orchestrating bioimage analysis workflows. Users chain discrete processing steps (**Tools**) into a Directed Acyclic Graph (**DAG**) where data flows between tools via DataFrames.

BioImageFlow addresses three challenges in bioimage analysis:

1. **Environment Isolation:** Tools often require conflicting dependencies (e.g., different Python versions, conflicting CUDA libraries). `ProcessingTool` classes declare `EnvironmentSpec` objects and run in isolated Conda environments when workflows use the Wetlands engine.
2. **Data Provenance:** Every execution is hashed and cached, making it possible to trace exactly which parameters and logic produced a specific result.
3. **Type Safety:** A rich typing system prevents wiring errors such as feeding a CSV file to a tool that expects a segmentation mask.

### 1.0 Baseline Contract

BioImageFlow targets Python `>=3.10` for the orchestrator and first-party tool packages.
`bioimageflow-core` targets Python `>=3.9` because it is injected into Wetlands worker environments, including external-binary environments whose dependencies require Python 3.9.
First-party packages are versioned independently and declare bounded compatibility requirements for other first-party distributions they use.
The repository root project is workspace-only: it exists to coordinate local package development and documentation, not as a runtime package imported by users.
Its `dev` dependency group therefore installs the workspace members and repository-only test, lint, and documentation tools.
Runtime dependencies belong in the `[project].dependencies` table of each distributable package, where every third-party requirement declares an explicit compatible lower bound.
Package-local documentation is source-only and is not part of the installed runtime API.
Public package exports are explicit through each package's `__all__`; names not exported there are internal unless documented otherwise.

### 1.1 Wetlands Integration

BioImageFlow relies on **Wetlands**, an external library for Conda environment isolation.

- **Wetlands** is a lightweight manager that creates Conda environments on demand from a dependency specification (e.g., `{"conda": ["cellpose==3.0"]}`).
- **BioImageFlow** is the orchestrator: it decides *what* to run and *in which order*. **Wetlands** is the executor: it spins up isolated environments and runs Python code inside them.
- Wetlands environments are created lazily on first use.
- By default they remain alive for one workflow execution, while an explicit engine ownership policy can retain them for an engine session or delegate their lifetime to an external manager.
- Communication between the main process and worker environments uses Python's `multiprocessing.connection`, so all transferred objects must be picklable.
- Exceptions raised in the worker are automatically re-raised in the main process with their original stack trace.
- BioImageFlow requires Wetlands `>=2.0.0,<3` and uses only its public top-level API.
- BioImageFlow translates its public `EnvironmentSpec` into the immutable Wetlands 2 `EnvironmentSpec`, provisions with `EnvironmentManager.provision(...).wait_for()`, and starts a `WorkerPool` with `ManagedEnvironment.start()`.
- Processing calls use `WorkerPool.submit_import("bioimageflow_core.worker:execute_processing_task", ...)`.
- `max_workers` selects the Wetlands 2 pool size. Node-effective `max_concurrent` bounds the number of row tasks BioImageFlow keeps active for that node.
- Wetlands 1 `worker_env` callbacks and automatic `CUDA_VISIBLE_DEVICES` assignment are not supported by Wetlands 2. Configuring `worker_env` produces an explicit migration error rather than silently changing execution.

For Wetlands API details, see [Appendix A: Wetlands API](#appendix-a-wetlands-api).

### 1.1.1 Public Distributed-Execution Integration Contract

The documented platform boundary is engine-neutral and consists of the following public values and operations:

- `NodeResourceOverrides`, `Node.resource_overrides`, `Node.set_resource_overrides()`, `Node.effective_resources`, and `effective_node_resources()` provide portable per-instance worker requirements for `ProcessingTool` nodes.
- `validate_parsl_config_ref()` resolves a trusted `ParslConfigRef` in an isolated child process, validates secrets, `retries=0`, and executor labels, returns sanitized structured diagnostics, and creates neither a DataFlowKernel nor a workflow run.
- `plan_distributed_execution()` compiles the same recursive processing scope and uses the same worker-requirement, capacity parser, compatibility, and route functions as Parsl startup. It records the validated task policy and creates no run, DataFlowKernel, provider allocation, scheduler job, Wetlands environment, or worker.
- `validate_remote_execution_profile()` invokes the public one-shot cluster protocol to validate connectivity, the remote factory, secrets, retries, executor labels, PSI/J availability, and cluster paths without creating a workflow run or scheduler job.
- `NodeFailureDiagnostic` is attached to failed `ProgressEvent` callbacks and is persisted as a separate diagnostic progress event for both `WorkflowRun.diagnostics()` and `RemoteWorkflowRun.diagnostics()`.
- `prepare_remote_submission()` copies all explicit `LocalUpload` bytes into an owned immutable bundle and returns a single-use `PreparedRemoteSubmission`. Submission verifies and consumes that bundle and never rereads the original paths.
- `get_execution_capabilities()` reports Direct, Wetlands, Parsl modes, PSI/J, planning, validation, diagnostics, resource overrides, and immutable-upload support without importing optional Parsl or PSI/J runtimes.

Every cross-process report has `to_dict()` and `from_dict()` methods and a versioned schema where applicable.
Scoped node paths are used consistently by planning, progress, and failure diagnostics.
Secrets and live Parsl objects are never serialized.
Resource placement values do not contribute to result or cache keys.

### 1.2 Package Architecture

BioImageFlow is split into two packages:

**`bioimageflow-core`** — The shared foundation. Installed in the main process **and** in every tool worker environment. Contains the type system, tool base classes (`BaseTool` and `ProcessingTool`), argument passing, and I/O dispatch helpers. It declares NumPy because shared-memory helpers expose NumPy array views, and it avoids orchestrator-only dependencies such as pandas, pydantic, and image IO stacks.

**`bioimageflow`** — The orchestrator. Installed only in the main process. Contains the graph engine, execution engines, column resolution, cache management, workflow coordination, and `DataFrameTool` base class. Depends on `bioimageflow-core`, `pandas`, `pydantic`, and the local runtime dependencies. The execution backends are `direct`, `wetlands`, and the optional, lazily imported `parsl` backend.

Common source, merge, conversion, and glue tools live in the first-party `bioimageflow-common-tools` companion package, not in the orchestrator package.

This split ensures that worker environments carry only the minimal footprint needed to run tool logic, while the main process has the full orchestration capabilities.

```text
bioimageflow-core (all environments)       bioimageflow (main process only)
├── types.py        # Type system          ├── dataframe_tool.py # DataFrameTool base class
├── environment.py  # EnvironmentSpec      ├── registry.py       # Tool registry
├── tool.py         # BaseTool,            ├── resolution.py     # Column resolver
│                   #   ProcessingTool,    ├── template.py       # Output templating
│                   #   IOModel, GUIMeta   ├── cache/            # Hashing and publication
├── arguments.py    # Arguments            ├── storage/          # Records and output views
├── io.py           # I/O dispatch (*)     ├── node.py           # Node, ColumnRef
├── shm.py          # Shared memory (*)    ├── engine/           # Scheduling and dispatch
├── worker.py       # Worker entry point   ├── backends.py       # Processing backend seam
└── worker_protocol.py # Envelopes/origins ├── parsl/            # Optional Parsl backend
                                           ├── worker_origins.py # Worker-origin resolution
                                           ├── events.py         # Progress event values
                                           ├── validation/       # Pydantic validation
                                           ├── tool_loader.py    # Versioned package loading
                                           └── workflow/         # Workflow coordination

(*) io.py and shm.py use NumPy at runtime and NumPy is a declared
    `bioimageflow-core` dependency so shared-memory APIs work in every
    worker environment.
```

The framework automatically adds `bioimageflow-core` to the dependencies of every Wetlands environment.

The orchestrator package exposes its final public imports explicitly while implementation modules remain focused.
The scheduler owns graph, cache, progress, and failure semantics; execution-specific processing dispatch is isolated behind the `ProcessingBackend` protocol so all backends use those semantics.

Pydantic-based validation of `Inputs`/`Outputs` is performed exclusively in the orchestrator (`bioimageflow` package), which does declare `pydantic` as a dependency. Worker environments never run Pydantic validation.

---

## 2. Type System

*Module: `bioimageflow_core.types`*

BioImageFlow uses a type system based on Python's `Annotated` types. Types carry metadata that enables compatibility checking between upstream outputs and downstream inputs.

### 2.1 Enumerations

```python
class Semantic(str, Enum):
    """What the pixel values represent."""
    BINARY = "binary"             # 0/1 (Masks)
    LABEL = "label"               # Integer IDs (Segmentation)
    INTENSITY = "intensity"       # Raw physical values (CT, MRI)
    PROBABILITY = "probability"   # 0.0-1.0 Floats
    DISPLACEMENT = "displacement" # Vector fields
    FEATURE = "feature"           # Embeddings

SCALAR_IMAGE_SEMANTICS = frozenset({
    Semantic.INTENSITY,
    Semantic.BINARY,
    Semantic.LABEL,
    Semantic.PROBABILITY,
})
"""Semantic values for scalar raster images.

Use this group for tools that consume displayable scalar images without
requiring a specific pixel meaning, such as visualization and montage tools.
It intentionally excludes vector fields and feature images.
"""

class Layout(str, Enum):
    """Axis ordering of the image data."""
    # 2D variants
    PLANAR = "YX"
    PLANAR_CHANNEL = "CYX"
    PLANAR_TIME = "TYX"
    PLANAR_TIME_CHANNEL = "TCYX"

    # 3D variants
    VOLUMETRIC = "ZYX"
    VOLUMETRIC_CHANNEL = "CZYX"
    VOLUMETRIC_TIME = "TZYX"

    # 4D variants
    VOLUMETRIC_TIME_CHANNEL = "TCZYX"

    @property
    def ndim(self) -> int:
        return len(self.value)
```

### 2.2 ImageSpec and SharedArray

```python
@dataclass(frozen=True)
class ImageSpec:
    """
    Defines type constraints (metadata attached to Annotated types).
    Empty sets mean 'any' (wildcard).
    """
    semantics: Set[Semantic] = field(default_factory=set)
    layouts: Set[Layout] = field(default_factory=set)
    dtypes: Set[str] = field(default_factory=set)       # e.g. {"uint8", "float32"}
    formats: Set[str] = field(default_factory=set)       # e.g. {".tif", ".nii.gz"}

@dataclass(frozen=True)
class SharedArray:
    """
    A reference to data in shared memory. Replaces Path when data is in RAM.
    Picklable — can cross the serialization boundary.
    """
    name: str                  # Key in shared memory (e.g., /dev/shm/bif_name)
    shape: Tuple[int, ...]
    dtype: str
```

### 2.3 Image Annotations

```python
def ImageShared(
    semantics=None, layouts=None, dtypes=None, gui=None
) -> Any:
    """Returns Annotated[SharedArray, ImageSpec(...), optional GUIMeta]."""
```

File-based image fields are declared directly as
`Annotated[Path, ImageSpec(...)]`. Add `GUIMeta(...)` as another `Annotated`
metadata entry when the field needs GUI hints. `ImageShared(...)` remains a
factory for shared-memory image fields; its constraint parameters accept a
single value, a set, or `None` (wildcard).

**Usage examples:**
```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import (
    Connectable,
    ImageShared,
    ImageSpec,
    GUIMeta,
    Layout,
    SCALAR_IMAGE_SEMANTICS,
    Semantic,
)

# File-based MRI input
MRI_File = Annotated[
    Path,
    ImageSpec(
        semantics={Semantic.INTENSITY},
        layouts={Layout.VOLUMETRIC},
        formats={".nii.gz"},
    ),
]

# Shared memory video stream
Video_Stream = ImageShared(semantics=Semantic.INTENSITY, layouts=Layout.PLANAR_TIME_CHANNEL, dtypes="uint8")

# Displayable scalar image input for visualization tools
Displayable_Image = Annotated[
    Path,
    ImageSpec(semantics=SCALAR_IMAGE_SEMANTICS),
]

# Image input with GUI metadata
Visible_Input = Annotated[
    Path,
    ImageSpec(semantics={Semantic.INTENSITY}),
    GUIMeta(display_name="Input image", connectable=Connectable.BY_DEFAULT),
]
```

### 2.4 Type Compatibility

Two types are **compatible** when their `ImageSpec` constraints overlap, checked per attribute (semantics, layouts, dtypes, formats) using **asymmetric wildcard** semantics:

| Producer | Consumer | Result |
|----------|----------|--------|
| any      | empty    | Compatible (consumer accepts anything) |
| empty    | non-empty | Compatible with **warning** (unverified) |
| non-empty | non-empty | Compatible only if sets intersect |

```python
def check_compatibility(producer_spec: ImageSpec, consumer_spec: ImageSpec) -> bool:
    """Returns True if the producer's output is acceptable for the consumer's input."""
    for attr in ["semantics", "layouts", "dtypes", "formats"]:
        producer_values = getattr(producer_spec, attr)
        consumer_values = getattr(consumer_spec, attr)
        if not consumer_values:
            continue
        if not producer_values:
            warnings.warn(f"Producer does not declare '{attr}'; cannot verify.")
            continue
        if not producer_values.intersection(consumer_values):
            return False
    return True
```

This check is used during [input binding](#46-input-binding-logic-graph-construction) to validate that a column reference's upstream type is compatible with the consuming input field's type.

`SCALAR_IMAGE_SEMANTICS` is only a convenience set for consumers that accept
several scalar image semantics. It does not change the compatibility relation:
a `BINARY` producer remains incompatible with a strict `INTENSITY` consumer
unless the consumer explicitly declares a set containing `BINARY`.

**Wire-shape serialization:** `bioimageflow.validation.serialize_image_spec(spec) -> dict | None` returns a JSON-friendly representation of an `ImageSpec` — `{"semantics": [...], "layouts": [...], "dtypes": [...], "formats": [...]}` with enum value strings (e.g. `"intensity"`, `"YX"`). This is the canonical shape for callers (GUIs, linters, documentation generators) that need to expose type information over the wire. `get_inputs_schema(tool)` includes it alongside the raw `ImageSpec` object under `image_spec_serialized`.

**Tool-level wire-shape serialization:** For a full per-field wire-format schema, callers should use `bioimageflow.validation.serialize_input_schema(tool_class) -> dict[str, dict]` and `serialize_output_schema(tool_class) -> dict[str, dict]`. Both accept the tool *class* (no instantiation is required) and return a fully JSON-serializable dict; both return `{}` when the tool has no `Inputs` / `Outputs` class attribute.

For per-tool (not per-field) facts, callers use `bioimageflow.validation.serialize_tool_metadata(tool_class) -> dict[str, Any]`. Returned keys: `tool_type` (`"ProcessingTool"` | `"DataFrameTool"`), `accepts_upstream` (bool — `True` for `ProcessingTool`; for `DataFrameTool` reflects the class attribute), `dynamic_outputs` (bool — `True` when the tool overrides `DataFrameTool.resolve_outputs` or `resolve_merge_schema`), and `dataframe_output` (bool — `True` when the node exposes its full result DataFrame as a graph-level output). GUIs use this to suppress the upstream pin on source DataFrameTools, render full-DataFrame output pins, and know whether to call `serialize_resolved_outputs(node)` for per-column output pins.

For *configured-node* output resolution, callers use `bioimageflow.validation.serialize_resolved_outputs(node) -> dict[str, Any]`. Returns `{"resolved": True, "columns": <schema>}` when the node's `get_output_schema()` resolves; otherwise `{"resolved": False, "columns": {}}`. The `columns` payload has the same shape as `serialize_output_schema` — either per-field entries or the `{"_passthrough": True, ...}` marker. GUIs use this to render per-column output pins on configured nodes (e.g. `Generate(column_name="x")` or fully-configured merge tools) and to know when to fall back to a placeholder pin.

For inputs, each field entry has exactly these keys:

```python
{
    "type": "float",                 # display-name string (see rules below)
    "required": True,                # bool; True iff no class-level default
    "nullable": False,               # bool; True iff the type admits None
    "connectable": "not_by_default", # "never" | "not_by_default" | "by_default"
    "default": 1.0,                  # JSON-safe default, or None
    "display_name": "Blur sigma",    # GUIMeta.display_name or None
    "description": "…",              # GUIMeta.description or None
    "group": "advanced",             # GUIMeta.group or None
    "min": 0.1,                      # GUIMeta.min or None
    "max": 50.0,                     # GUIMeta.max or None
    "step": 0.1,                     # GUIMeta.step or None
    "path_picker": "file",          # "file" | "folder" | "both" | None
    "choices": ["fast", "accurate"], # from Literal[...] / Enum, or None
    "image_spec": {...},             # serialize_image_spec(...) or None
}
```

The `type` display name follows deterministic rules: bare Python types use `__name__` (`"int"`, `"float"`, `"str"`, `"bool"`, `"Path"`); `list` / `dict` / `tuple` generics collapse to `"list"` / `"dict"` / `"tuple"`; `Literal[...]` uses the type of the first literal (the enumeration is carried by `choices`, not `type`); `Enum` subclasses become `"str"`; `Annotated[X, ...]` unwraps to `X`; `Optional[X]` / `X | None` uses the display name of `X` (None-ness is expressed by `required`, not by `type`); `Annotated[Path, ImageSpec(...)]` and `ImageShared(...)` emit `"ImageFile"` and `"ImageShared"` respectively. The reserved value `"any"` denotes a column whose runtime type is unknown — emitted by `resolve_outputs` / `resolve_merge_schema` for dynamic columns whose name (but not concrete type) is known at graph-construction time, and by `Concat.resolve_merge_schema` when two upstream schemas declare the same column with conflicting types.

The `connectable` field uses three-state strings: `"never"` (no pin, no toggle), `"not_by_default"` (pin hidden by default, a GUI checkbox reveals it), and `"by_default"` (pin visible by default, a GUI checkbox can hide it). Callers that only care whether a field has a pin should treat both `"not_by_default"` and `"by_default"` as connectable.

The `path_picker` field is a GUI-only hint for path-typed inputs: `"file"` offers file selection, `"folder"` offers folder selection, and `"both"` offers both actions. `None` leaves the choice to the GUI's type-based inference. It does not validate whether a runtime value exists or is a file or directory.

`required`, `nullable`, and the type-display rules are three orthogonal concerns:

- `required` is determined solely by presence of a class-level default on `Inputs`: a field with no default is `required=True`, even when its type is `Optional[X]` or `X | None`. A caller of a tool whose field is typed `Optional[int]` with no default must pass *something* — but `None` is acceptable when `nullable=True`.
- `nullable` is determined solely by the type annotation: `True` iff the annotation (after unwrapping `Annotated[...]`) is a `Union` whose args include `NoneType`. It is independent of whether a default exists. GUIs should use `nullable` (not `required`) to decide whether to expose a "set to null" affordance.
- The `type` display name strips `None` from unions — `Optional[int]` displays as `"int"` — because the None-ness is carried by `nullable`, not by `type`.

Output fields are simpler: `{"type": str, "default": Any | None, "image_spec": dict | None, "template": str | None}`. `template` is present when a `ProcessingTool` path output declares a `Template(...)` default. If an output annotation carries `GUIMeta`, the serialized output entry also includes JSON-safe `GUIMeta` fields (`connectable`, `display_name`, `description`, `group`, `min`, `max`, `step`) so GUIs can label output pins and tooltips. When `Outputs` is a `Passthrough` subclass (see §3.5 `DataFrameTool`), `serialize_output_schema` returns the marker `{"_passthrough": True}` — GUIs should render this as "inherits upstream columns".

Callers that want the Python-facing objects (raw `type`, raw `Connectable`) should keep using `get_inputs_schema(tool)` instead; the two APIs are complementary.

### 2.5 Interface Type Constraints

`Inputs` and `Outputs` models must use only standard-library types and `bioimageflow-core` metadata types such as `ImageSpec`, `GUIMeta`, and `ImageShared`. File-based image fields use `Annotated[Path, ImageSpec(...)]`. Third-party types (NumPy arrays, PIL images, etc.) are **not** allowed in the interface — they cannot cross the serialization boundary. `Outputs` is required on `ProcessingTool` (defines the serialization contract and output templates). On `DataFrameTool`, `Outputs` is optional — when declared, it enables construction-time validation of downstream column references (see [Section 3.5](#35-dataframetool)).

**Runtime type resolution:** File-based image annotations and `ImageShared` are distinct for graph-level compatibility checking (`check_compatibility`), but the orchestrator's Pydantic model builder resolves both to `Union[Path, str, SharedArray]` at validation time. This is necessary because caching may convert a `SharedArray` output to a file `Path` (see [Section 8.2](#82-lifecycle)), and the reverse can happen when shared memory is enabled. Tools should use `load_image()` which handles both transparently.

---

## 3. Tool Definition

BioImageFlow provides two kinds of tools, each with a single execution context:

- **`ProcessingTool`** — runs computation in an isolated Wetlands environment. Every method the tool author implements (`process_row`, `process_batch`) executes in the worker.
- **`DataFrameTool`** — transforms DataFrames in the main process. The single `transform` method has full access to Pandas.

Both inherit from `BaseTool`, which provides shared metadata attributes (`display_name`, `documentation`, `category`, `tags`, `Inputs`) and graph wiring via `__call__`.

### 3.1 EnvironmentSpec

*Module: `bioimageflow_core.environment`*

Processing tools declare their environment requirements via an `EnvironmentSpec` object. This object is defined **once** and shared by reference across all tools that use the same environment.

```python
@dataclass(frozen=True)
class EnvironmentSpec:
    """Defines a reusable Wetlands environment specification."""
    name: str          # Wetlands environment name (e.g., "cellpose")
    dependencies: dict  # Wetlands format: {"conda": [...], "pip": [...], "python": "3.12"}
    allow_flexible_versions: bool = False
```

**Defining an environment:**
```python
from bioimageflow_core import EnvironmentSpec

cellpose_env = EnvironmentSpec(
    name="cellpose",
    dependencies={"conda": ["cellpose==4.0.8"], "python": "3.12"}
)

stardist_env = EnvironmentSpec(
    name="stardist",
    dependencies={"conda": ["stardist==0.9", "tensorflow>=2,<3"], "python": "3.11"},
    allow_flexible_versions=True,
)
```

`EnvironmentSpec` validates package-index dependencies when it is constructed.
By default, entries in `pip` and `conda` lists use exact pins; bare names such as `"numpy"` or `"tensorflow"` raise `ValueError`.
Set `allow_flexible_versions=True` only when the environment intentionally accepts resolver flexibility; in that mode explicit constraints such as `">=2,<3"` or `"~=1.2"` are accepted, but bare names still raise.
Direct references such as `"bioimageflow-core @ file:///..."` and Wetlands local dependency dicts are already anchored and do not need a package-index version specifier.

Multiple tools can reference the same `EnvironmentSpec`. BioImageFlow validates the workflow graph before execution: reachable tools sharing the same `name` must declare identical `dependencies`, or `EnvironmentMismatchError` is raised with the conflicting tool names and dependency declarations. During Wetlands execution, BioImageFlow translates the augmented environment recipe to the Wetlands 2 immutable specification, calls `EnvironmentManager.provision(...).wait_for()`, and lets Wetlands validate whether a ready same-name managed environment can be reused.

**Dependency normalization:** For cache and provenance hashing, the framework normalizes the dependency specification to avoid false cache misses:
- Dependency lists are sorted alphabetically (e.g., `["numpy==2.4.2", "cellpose==3.1.1.1"]` and `["cellpose==3.1.1.1", "numpy==2.4.2"]` produce the same hash).
- Version strings are normalized to PEP 440 canonical form (e.g., `"3.0"` and `"3.0.0"` are treated as equivalent).
- Whitespace is stripped from dependency strings.

It is possible to directly define the EnvironmentSpec in ProcessingTool.environment if only one tool requires the environment.

#### Pre-Built General Environment

*Module: `bioimageflow_core.environment`*

`bioimageflow-core` provides a pre-defined `GENERAL_ENV` constant — a standard scientific image processing environment that covers the majority of "glue" tools. Tools that only need common packages (numpy, scipy, scikit-image, imageio, tifffile, Pillow, pandas) should use `GENERAL_ENV` instead of declaring ad-hoc environments.

```python
from bioimageflow_core import GENERAL_ENV

GENERAL_ENV = EnvironmentSpec(
    name="bioimageflow-general",
    dependencies={
        "python": "3.12",
        "pip": [
            "numpy==2.5.0",
            "scipy==1.18.0",
            "scikit-image==0.26.0",
            "imageio==2.37.3",
            "tifffile==2026.6.1",
            "Pillow==12.3.0",
            "pandas==3.0.3",
        ]
    }
)
```

**When to use `GENERAL_ENV`:** Tools whose only runtime dependencies are a subset of the packages above, or the Python standard library.
For example, a tool that reads an image with imageio, processes it with numpy, and writes it back should use `GENERAL_ENV`.
So should simple source or utility tools such as HTTP downloads implemented with `urllib`, path normalization, CSV/table glue, and small file operations.
Do not create one-off environments for these tasks.

**When NOT to use `GENERAL_ENV`:** Tools that require specialized libraries (cellpose, stardist, SimpleITK, bioio, opencv, etc.) still declare their own `EnvironmentSpec`. The general env catches the long tail of tools that just need standard scientific Python.

**Engine behavior:** `GENERAL_ENV` is a regular `EnvironmentSpec` — no sentinel, no magic. The engine provisions it on first use and reuses one cached Wetlands 2 worker pool for all tools referencing it. The pool size follows the workflow environment configuration.

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import ProcessingTool, GENERAL_ENV, IOModel, Arguments, ImageSpec, Semantic, Template

class ExtractChannel(ProcessingTool):
    display_name = "Extract Channel"
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        channel: int = 0

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})] = Template("{input_image.stem}_ch{channel}{ext}")

    def process_row(self, arguments: Arguments) -> "Outputs":
        import imageio.v3 as iio
        ...
```

### 3.2 Category

*Module: `bioimageflow_core.tool`*

`Category` is a `str` enum that classifies tools into high-level functional areas. It is optional — tools that don't fit a predefined category can leave it as `None`. Unlike `tags` (free-form, multiple per tool), `category` assigns exactly one canonical function to a tool, making it suitable for UI grouping and filtering.

```python
class Category(str, Enum):
    """High-level functional category for a tool."""
    CONVERSION = "conversion"
    IMAGE_PROCESSING = "image_processing"
    SEGMENTATION = "segmentation"
    REGISTRATION = "registration"
    SPECTRAL_ANALYSIS = "spectral_analysis"
    TRACKING = "tracking"
    MEASUREMENT = "measurement"
    SPOT_DETECTION = "spot_detection"
    DECONVOLUTION = "deconvolution"
    RESTORATION = "restoration"
    COLOCALIZATION = "colocalization"
    STITCHING = "stitching"
    CLASSIFICATION = "classification"
    UTILITIES = "utilities"
```

| Value                | Description                                      |
|---------------------|--------------------------------------------------|
| `CONVERSION`         | Format conversion (file types, bit depth, etc.)  |
| `IMAGE_PROCESSING`   | General image processing (filtering, transforms) |
| `SEGMENTATION`       | Object / region segmentation                     |
| `REGISTRATION`       | Spatial alignment and registration               |
| `SPECTRAL_ANALYSIS`  | Spectral unmixing, channel analysis              |
| `TRACKING`           | Object tracking across time                      |
| `MEASUREMENT`        | Measurement and quantification                   |
| `SPOT_DETECTION`     | Spot / puncta detection                          |
| `DECONVOLUTION`      | Image deconvolution                              |
| `RESTORATION`        | Restoration and super-resolution                 |
| `COLOCALIZATION`     | Colocalization analysis                          |
| `STITCHING`          | Image stitching / montage assembly               |
| `CLASSIFICATION`     | Image or object classification                   |
| `UTILITIES`          | General-purpose utilities                        |

### 3.3 BaseTool

*Module: `bioimageflow_core.tool`*

`BaseTool` is the abstract base class for all tools. It lives in `bioimageflow-core` and provides the common foundation shared by both `ProcessingTool` and `DataFrameTool`.

```python
class BaseTool(ABC):
    """
    Common base for all tools. Provides identity and Inputs.
    Not instantiated directly — use ProcessingTool or DataFrameTool.
    __call__ is NOT defined here — each subclass defines its own calling
    convention to avoid Liskov Substitution violations (ProcessingTool
    accepts keyword-only args; DataFrameTool accepts positional + keyword).
    """
    name: str                       # Unique identifier for the tool
    documentation: str = ""         # Human-readable description
    category: Category | None = None  # High-level functional category
    tags: list[str] = []            # Searchable tags

    class Inputs(IOModel): ...      # Declared by each concrete tool
```

`__call__` is defined on each subclass (`ProcessingTool`, `DataFrameTool`) rather than on `BaseTool`, because the calling conventions differ: `ProcessingTool` accepts only keyword arguments (column references, node shorthand, or constants); `DataFrameTool` accepts positional arguments (upstream nodes) and keyword arguments (`Inputs` parameters). Both use a lazy import guard so that the method exists in worker environments but raises a clear error if accidentally invoked there (see below).

GUIs exposing a tool's schema over the wire should use `bioimageflow.validation.serialize_input_schema(tool_class)` and `serialize_output_schema(tool_class)` — the canonical, JSON-safe representation (see §2.4).

### 3.4 ProcessingTool

*Module: `bioimageflow_core.tool`*

`ProcessingTool` is the base class for tools that process data in an isolated Wetlands environment. Every method the tool author implements runs in the worker — there are no main-process hooks on this class.

```python
class ProcessingTool(BaseTool):
    """
    Tool that processes data in an isolated Wetlands environment.
    All custom methods (process_row, process_batch) run in the worker.
    """
    environment: EnvironmentSpec    # Required — defines the Wetlands environment
    run_empty_batch: bool = False   # Opt-in reducer/artifact behavior for zero rows
    empty_batch_anchor_inputs: tuple[str, ...] = ()

    class Outputs(IOModel): ...     # Declared by each concrete tool

    def __call__(self, *, name: str | None = None, **kwargs) -> "Node":
        """Create a graph node. No computation occurs.
        name: optional custom node name (default: auto-generated).
        kwargs: ColumnRef, Node shorthand, or constants.
        Only usable in the orchestrator process.
        """
        try:
            from bioimageflow.node import Node
        except ImportError:
            raise RuntimeError(
                f"{type(self).__name__}.__call__() requires the bioimageflow "
                f"orchestrator package. This method is not available in worker "
                f"environments — use process_row/process_batch instead."
            )
        return Node(tool=self, kwargs=kwargs, name=name)

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ) -> "Outputs | list[Outputs]":
        """
        Process a single row. Runs in the worker environment.

        Returns:
            - Single Outputs: 1-to-1 mapping (common case).
            - list[Outputs]: 1-to-N mapping. The engine explodes the DataFrame,
              creating child indices for each output. The tool is responsible for
              generating non-colliding file paths.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement process_row or process_batch."
        )

    def process_batch(
        self,
        arguments_list: "list[Arguments]",
        *,
        context: ExecutionContext | None = None,
    ) -> "list[list[Outputs]] | list[Outputs]":
        """
        Process all rows at once. Runs in the worker environment.
        Override for batch processing (e.g., GPU inference, training).

        Returns:
            - list[list[Outputs]]: one inner list per input row (supports 1-to-N).
            - list[Outputs]: shorthand for 1-to-1 batch tools (one output per row).
              The engine auto-wraps each element in a singleton list.

        If not overridden, the engine falls back to per-row processing
        via process_row. The engine detects overrides using:
            type(tool).process_batch is not ProcessingTool.process_batch
        """
        raise NotImplementedError  # Never called — engine checks override first
```

Concrete `ProcessingTool` subclasses must override at least one of `process_row` or `process_batch`. The framework validates this via `__init_subclass__` and raises `TypeError` at class definition time if neither is overridden.

Batch tools are not called when their row-aligned upstream inputs are empty by default; the engine publishes an empty output dataframe with the declared output columns. Reducer or artifact-rendering batch tools that can produce a meaningful aggregate output for zero rows may set `run_empty_batch = True`. In that case the engine calls `process_batch` with synthetic argument rows built from constants, defaults, output templates, and any `empty_batch_anchor_inputs` bound to non-empty upstream columns. Without anchors, the engine supplies one synthetic argument row. With anchors, it supplies one synthetic argument row per anchor row. Anchor inputs are for non-row context such as a source label image used to render an all-background output; they must not be used to create fake object or spot rows.

**Progress reporting:** `process_row` may declare an optional keyword parameter `task` to receive a `RemoteTaskHandle` for sub-row progress reporting. When present, Wetlands injects the handle automatically. Tools that don't declare `task` are unaffected.

```python
class MySegmenter(ProcessingTool):
    def process_row(self, arguments: Arguments, *, task=None) -> Outputs:
        tiles = split_tiles(arguments.input_image, n=20)
        for i, tile in enumerate(tiles):
            if task:
                task.update(f"Processing tile {i+1}/{len(tiles)}",
                            current=i, maximum=len(tiles))
            result = self.model.predict(tile)
        return self.Outputs(...)
```

The `task` parameter also provides cooperative cancellation via `task.cancel_requested` (see [Cancellation](#13-cancellation)).

**Execution scratch context:** `process_row` and `process_batch` may declare an optional keyword-only `context: ExecutionContext` parameter. The engine injects it only when the method explicitly declares `context`; existing tools with `process_row(arguments)` or `process_batch(arguments_list)` are unchanged.

`ExecutionContext` is defined in `bioimageflow-core` and is picklable across the Wetlands serialization boundary:

```python
@dataclass(frozen=True)
class ExecutionContext:
    run_dir: Path       # storage_path/cache/v1/results/<shard>/<result-key>/attempts/<attempt-id>/staging/
    assets_dir: Path    # run_dir/assets/
    work_dir: Path      # shared node-level runtime directory, run_dir/work/
    rows_dir: Path      # shared row scratch parent, run_dir/work/rows/
    row_dir: Path | None = None    # private process_row scratch directory
    batch_dir: Path | None = None  # private process_batch scratch directory
    row_index: str | None = None  # original input row index for process_row
```

`context.work_dir` is shared by every call for the node and always points to `run_dir/work/`. `context.rows_dir` is the shared row scratch parent, `run_dir/work/rows/`. For `process_row`, `context.row_dir` is the private scratch directory for that row: `run_dir/work/rows/<safe_row_id>/`. For `process_batch`, `context.batch_dir` is the private batch scratch directory: `run_dir/work/batch/`.

Runtime scratch directories are for intermediate and implicit runtime files only. Declared outputs must still be written to paths from `Arguments` and returned through `Outputs`. Tools wrapping external binaries that create files relative to their current directory should pass `cwd=context.row_dir` from `process_row` or `cwd=context.batch_dir` from `process_batch` to `subprocess.run()` or equivalent. Shared generated runtime resources that are reused across rows should be placed under `context.work_dir`, preferably in a tool-named child directory. The engine must not use process-wide `os.chdir()`, because direct execution can run nodes in threads.

**Runtime path contract:** Before dispatch, the orchestrator converts the workflow storage root, every `ExecutionContext` directory, every generated `ProcessingTool` output path, and every path-typed `Arguments` value to an absolute runtime path. Relative user-supplied path constants are interpreted once in the orchestrator process, before the tool is called. DataFrame columns declared as path-typed outputs are also stored as absolute paths. Tool implementations may pass framework-provided path arguments directly to file I/O libraries or subprocesses, even when the subprocess runs with `cwd=context.row_dir` or `cwd=context.batch_dir`; tools must not call `resolve()` merely to compensate for framework-relative paths.

**Direct tool definition:**
```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import ProcessingTool, IOModel, ImageSpec, Semantic, Arguments, Category, Template

class MySegmenter(ProcessingTool):
    display_name = "My Segmenter"
    documentation = "Segments cells."
    category = Category.SEGMENTATION
    tags = ["segmentation"]
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("{input_image.stem}_mask_{row_index}.png")
        cell_count: int

    def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
        import cellpose.models
        ...
```

**Tool families via inheritance:**

Tools of the same family often share the same environment. A base class defines the environment (and optionally shared tags, helpers, etc.), and child classes inherit it:

```python
class CellposeBase(ProcessingTool):
    """Base class for all Cellpose-family tools. Defines the shared environment."""
    environment = cellpose_env
    tags = ["cellpose"]

class CellposeSegmenter(CellposeBase):
    display_name = "Cellpose Segmenter"
    documentation = "Segments cells using the Cellpose algorithm."

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("{input_image.stem}_mask_{row_index}.png")
        cell_count: int

    def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
        import cellpose.models
        ...

class CellposeTrain(CellposeBase):
    display_name = "Cellpose Train"
    documentation = "Trains a custom Cellpose model."
    tags = ["cellpose", "training"]

    class Inputs(IOModel):
        training_images: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        training_masks: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        epochs: int = 100

    class Outputs(IOModel):
        model_path: Path = Template("{node_name}_model_{timestamp}")

    def process_batch(self, arguments_list: list[Arguments]) -> list[Outputs]:
        import cellpose.models
        ...  # Returns list[Outputs] — one output per row (auto-wrapped)
```

**Inner class inheritance:** `Inputs` and `Outputs` are inner classes that do **not** automatically inherit from the parent's inner classes. If a tool family shares common input fields, the child must explicitly inherit: `class Inputs(CellposeBase.Inputs)`. `IOModel._get_all_annotations()` walks the MRO, so inherited fields are resolved correctly.

**A tool not related to cellpose can still share the environment directly:**
```python
class SomeOtherTool(ProcessingTool):
    display_name = "Other Tool"
    environment = cellpose_env  # Reuses the cellpose environment without inheriting
    ...
```

**ProcessingTool class attributes:**

| Attribute       | Type               | Description                                        |
|----------------|--------------------|----------------------------------------------------|
| `display_name`  | `str`              | Human-readable tool label; falls back to class name when empty |
| `documentation` | `str`              | Human-readable description                         |
| `category`      | `Category \| None` | High-level functional category (optional)          |
| `tags`          | `list[str]`        | Searchable tags                                    |
| `environment`   | `EnvironmentSpec`  | Wetlands environment specification (shared object) |
| `resources`     | `ResourceSpec`     | Optional resource requirements (GPU, memory, concurrency). See [Section 10](#10-resource-constraints). |

**Worker state warning:** State set on `self` during `__init__` (graph construction, main process) is **not** available in `process_row`/`process_batch` (worker process). For expensive resources like GPU models, use lazy initialization inside the processing method:

```python
class MyTool(ProcessingTool):
    _model = None
    _model_key = None

    def process_row(self, arguments):
        key = arguments.model_name
        if self._model is None or self._model_key != key:
            self.clear_model_cache()
            self._model = load_model(key)  # Lazy init in worker
            self._model_key = key
        result = self._model.predict(...)

    def clear_model_cache(self):
        self._model = None
        self._model_key = None
```

Model caches should be bounded and keyed by every argument that changes model construction.
Inference-only arguments should not invalidate weights.

### 3.5 DataFrameTool

*Module: `bioimageflow.dataframe_tool`*

`DataFrameTool` is the base class for tools that transform DataFrames in the main process (no isolated environment). It provides two methods: `merge_dataframes` for combining upstream DataFrames, and `transform` for operating on the merged result. It lives in the `bioimageflow` package.

DataFrameTool calls use **positional arguments** for upstream nodes (whose output DataFrames are passed to `merge_dataframes`) and **keyword arguments** for `Inputs` parameters (constants).

GUIs exposing a tool's schema over the wire should use `bioimageflow.validation.serialize_input_schema(tool_class)` and `serialize_output_schema(tool_class)` — the canonical, JSON-safe representation (see §2.4). Tools that declare `class Outputs(Passthrough): pass` are serialized as the marker `{"_passthrough": True}`, signalling to the UI that the tool inherits upstream columns.

```python
from bioimageflow import DataFrameTool

class DataFrameTool(BaseTool):
    """
    Tool that transforms DataFrames. Two-phase lifecycle: merge upstream DataFrames, then transform.
    Optional Outputs for construction-time validation.
    """

    # Optional: declare Outputs(IOModel) for construction-time column validation.
    # If omitted, column validation is deferred to execution time.
    # Use class Outputs(Passthrough): pass if the tool preserves all input columns.

    def __call__(self, *upstream_nodes, name: str | None = None, **kwargs) -> "Node":
        """Create a graph node. No computation occurs.
        positional: upstream Nodes (passed to merge_dataframes).
        name: optional custom node name (default: auto-generated).
        kwargs: Inputs constants.

        Positional argument ordering follows left-to-right convention
        (matching Pandas/SQL): the first argument is the 'left' table,
        subsequent arguments are joined to it sequentially.
        """
        try:
            from bioimageflow.node import Node
        except ImportError:
            raise RuntimeError(
                f"{type(self).__name__}.__call__() requires the bioimageflow "
                f"orchestrator package."
            )
        return Node(tool=self, args=upstream_nodes, kwargs=kwargs, name=name)

    def merge_dataframes(self, dfs: "list[DataFrame]", arguments: "Arguments") -> "DataFrame":
        """
        Combine upstream DataFrames into one.
        Default: inner join on index.

        Args:
            dfs: Output DataFrames from upstream nodes (one per positional arg).
            arguments: Resolved Inputs values (constants).

        Returns:
            A single merged DataFrame.
        """
        # Default: inner join on index (same as InnerJoin — see built-in merge tools below)

    def transform(self, df: "DataFrame", arguments: "Arguments") -> "DataFrame":
        """
        Transform the merged DataFrame.

        Args:
            df: The merged upstream DataFrame (from merge_dataframes).
            arguments: Resolved Inputs values (constants).

        Returns:
            A new or modified DataFrame. May have different rows, columns,
            or index than the input.
        """
        return df  # Default: identity (passthrough)
```

#### Optional `Outputs` for Construction-Time Validation

`DataFrameTool` has a dynamic output schema — whatever `transform()` returns. This means column validation for downstream ColumnRefs is deferred to execution time by default. To enable construction-time validation, tools can optionally declare `Outputs` — the same `IOModel` mechanism used by `ProcessingTool`:

```python
class FilterRows(DataFrameTool):
    display_name = "Filter Rows"

    class Outputs(Passthrough): pass  # Output schema = input schema (all columns preserved)

class CountLabelOverlaps(DataFrameTool):
    display_name = "Count Label Overlaps"

    class Outputs(IOModel):
        image1: str
        label1: int
        label2_count: int
```

Three modes:
- **No `Outputs`** (default): Column validation is deferred to execution time. Use when the output schema is dynamic (e.g., `ColumnRegex`, where columns depend on the regex).
- **`class Outputs(Passthrough)`**: The tool preserves all input columns. `Passthrough` is a special base class provided by `bioimageflow` (alongside `IOModel`). The engine uses the upstream schema for validation. New fields can be declared on `Passthrough` subclasses to indicate columns added by the tool: `class Outputs(Passthrough): cell_count: int`. The engine merges these with the upstream schema for construction-time validation.
- **`class Outputs(IOModel)`**: Explicit output schema. The engine validates downstream ColumnRefs against this declaration at construction time. Supports full `IOModel` annotations including `Annotated[Path, ImageSpec(...)]` and `ImageShared` type metadata for downstream type compatibility checks.

The execution lifecycle for DataFrameTool is:
1. Collect upstream DataFrames (from positional arguments)
2. Resolve `Inputs` parameters into a single `Arguments` object (all constants)
3. Call `merge_dataframes(dfs, arguments)` → merged DataFrame
4. Call `transform(df, arguments)` → final output DataFrame

A merge-only tool overrides `merge_dataframes` and keeps the default `transform` (identity). A transform-only tool overrides `transform` and keeps the default `merge_dataframes` (inner join). A tool that does both overrides both methods.

**DataFrameTool class attributes:**

| Attribute       | Type                                    | Description                                    |
|----------------|-----------------------------------------|------------------------------------------------|
| `display_name`  | `str`                                   | Human-readable tool label; falls back to class name when empty |
| `documentation` | `str`                                   | Human-readable description                     |
| `category`      | `Category \| None`                      | High-level functional category (optional)      |
| `tags`          | `list[str]`                             | Searchable tags                                |
| `accepts_upstream` | `bool` (default `True`)              | Whether the tool accepts positional upstream `Node`s. Set to `False` on source tools (`Files`, `Generate`). |
| `Outputs`       | `IOModel subclass \| Passthrough subclass \| —` | Optional output schema for construction-time validation (see above) |

Source DataFrameTools (`accepts_upstream = False`) do not accept positional arguments. Construction with positional arguments raises `SourceToolUpstreamError`. `Files` and `Generate` are the canonical source tools.

**Dynamic output schema.** Tools whose output column names depend on their inputs (e.g. `Generate`, where `column_name` is a runtime parameter) override the `resolve_outputs(cls, inputs)` classmethod:

```python
class Generate(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        column_name: str
        values: list[Any]

    @classmethod
    def resolve_outputs(cls, inputs=None):
        name = (inputs or {}).get("column_name")
        if not name:
            return None
        return {name: {"type": "any", "default": None, "image_spec": None}}
```

`resolve_outputs` returns a dict whose values match the per-field shape produced by `serialize_output_schema` (`{"type": str, "default": Any | None, "image_spec": dict | None}`), or `None` when the schema is unresolvable from the supplied inputs. The default implementation delegates to `serialize_output_schema(cls)` for tools that declare a static `Outputs` class. Implementations must be pure (no I/O, no side effects).

Common-tools merge tools (`InnerJoin`, `CrossJoin`, `JoinOnColumn`, `Concat`, `Collect`) instead override `resolve_merge_schema(cls, upstream_schemas, inputs)` because their output columns depend on the *upstream* schemas, not just on their own inputs. The `Node.get_output_schema()` algorithm (next paragraph) prefers `resolve_merge_schema` over `resolve_outputs` when a merge tool overrides it.

**`Node.get_output_schema()`.** Public method on `Node` that resolves the node's output schema as currently configured. Algorithm:

1. If the tool overrides `resolve_merge_schema` (i.e. is a built-in merge tool), collect upstream schemas via each positional arg's `get_output_schema()` and call `tool.resolve_merge_schema(upstream_schemas, kwargs)`. Return whatever it returns.
2. Otherwise, on a `DataFrameTool`, call `tool.resolve_outputs(kwargs)`.
3. On a `ProcessingTool`, return `serialize_output_schema(type(tool))`.
4. Returns `None` when the schema is unresolvable (any required upstream returns `None`).

`get_output_schema` is idempotent and side-effect free; safe to call repeatedly. Construction-time `ColumnRef` validation (`node["col"]`) consults `get_output_schema` so that dynamic-but-resolvable schemas (e.g. `Generate(column_name="x")["x"]`, or a fully-configured `CrossJoin`) validate without runtime deferral.

**DataFrameTool examples:**

```python
from bioimageflow import DataFrameTool, Passthrough
from bioimageflow_core import IOModel, Arguments

class ColumnRegex(DataFrameTool):
    """Create dynamically named columns from a regex pattern."""
    display_name = "Column Regex"
    tags = ["dataframe", "regex"]

    class Inputs(IOModel):
        column_name: str
        regex: str = r'(?P<column1>\w+)_(?P<column2>\w+)'

    def transform(self, df, arguments):
        import re
        df = df.copy()
        for index, row in df.iterrows():
            m = re.search(arguments.regex, str(row[arguments.column_name]))
            if m:
                for key, value in m.groupdict().items():
                    df.at[index, key] = value
        return df


class FilterRows(DataFrameTool):
    """Filter DataFrame rows by column value constraints."""
    display_name = "Filter Rows"
    tags = ["dataframe", "filter"]

    class Outputs(Passthrough): pass  # All input columns are preserved

    class Inputs(IOModel):
        column_name: str
        min: float | None = None
        max: float | None = None
        numbers_to_remove: str | None = None

    def transform(self, df, arguments):
        if arguments.min is not None:
            df = df[df[arguments.column_name] >= arguments.min]
        if arguments.max is not None:
            df = df[df[arguments.column_name] <= arguments.max]
        if arguments.numbers_to_remove is not None:
            numbers = [float(n) for n in arguments.numbers_to_remove.split(",")]
            df = df[~df[arguments.column_name].isin(numbers)]
        return df


class CountLabelOverlaps(DataFrameTool):
    """Count the number (or average number) of overlapping labels."""
    display_name = "Count Label Overlaps"
    tags = ["aggregation"]

    class Inputs(IOModel):
        label1_min: float | None = None
        label1_max: float | None = None
        average: bool = False

    class Outputs(IOModel):
        image1: str
        label1: int
        label2_count: int

    def transform(self, df, arguments):
        if arguments.label1_min is not None:
            df = df[df['label1'] >= arguments.label1_min]
        if arguments.label1_max is not None:
            df = df[df['label1'] <= arguments.label1_max]
        if not {'label1', 'image1', 'label2'}.issubset(df.columns):
            import pandas as pd
            return pd.DataFrame()
        result = df.groupby(['image1', 'label1'])['label2'].agg(
            lambda x: (x != 0).sum()
        ).reset_index(name="label2_count")
        if arguments.average:
            return result.groupby('image1')['label2_count'].mean().reset_index(
                name='average_number_of_label2_per_label1'
            )
        return result
```

**Built-in merge DataFrameTools:**

BioImageFlow provides DataFrameTools for common merge operations through the `bioimageflow_common_tools` companion package. These override `merge_dataframes` and use the default `transform` (identity):

```python
class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index (default merge behavior)."""
    display_name = "Inner Join"

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs, arguments):
        if not dfs:
            import pandas as pd
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.join(df, how="inner", rsuffix="__bif_dup")
            result = result[[c for c in result.columns if not c.endswith("__bif_dup")]]
        return result


class CrossJoin(DataFrameTool):
    """Cross join for combinatorial expansion."""
    display_name = "Cross Join"
    class Inputs(IOModel):
        suffixes: tuple = ("_left", "_right")


class JoinOnColumn(DataFrameTool):
    """Join upstream DataFrames on a named column (not index)."""
    display_name = "Join On Column"
    class Inputs(IOModel):
        join_column: str
        how: str = "inner"
        suffixes: tuple = ("_left", "_right")


class Concat(DataFrameTool):
    """Concatenate DataFrames vertically."""
    display_name = "Concat"
    class Inputs(IOModel):
        pass


class Collect(DataFrameTool):
    """Gather columns from multiple ancestor nodes into one DataFrame.
    Convenience alias for InnerJoin — makes intent explicit when combining
    scattered columns from different pipeline branches."""
    display_name = "Collect"
    class Outputs(Passthrough): pass
    class Inputs(IOModel):
        pass
    # Uses default merge_dataframes (inner join on index) and default transform (identity)
```

`Collect` is useful when downstream code needs columns from many ancestors without manual ColumnRef wiring for each one:

```python
# Gather columns from multiple ancestors into one DataFrame
all_data = Collect()(raw, masks, stats)
export = save(
    image=all_data["path"],
    mask=all_data["mask"],
    mean_intensity=all_data["mean_intensity"]
)
```

### 3.5 IOModel and Inputs/Outputs

*Module: `bioimageflow_core.tool`*

`Inputs` and `Outputs` are declared as inner classes extending `IOModel`, a lightweight pure-Python base class provided by `bioimageflow-core`. `IOModel` supports field declarations via annotations, default values, and construction from keyword arguments — but performs **no validation itself**. Validation is handled by the orchestrator using Pydantic (see below).

```python
class IOModel:
    """
    Lightweight declarative base for tool Inputs/Outputs.
    No pandas or pydantic dependency; uses only standard-library features.
    """
    @classmethod
    def _get_all_annotations(cls):
        """Walk the MRO to collect annotations from all ancestor classes."""
        annotations = {}
        for klass in reversed(cls.__mro__):
            annotations.update(getattr(klass, '__annotations__', {}))
        return annotations

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self._get_all_annotations())
        if unknown:
            raise TypeError(f"Unknown fields: {unknown}")
        for name in self._get_all_annotations():
            if name in kwargs:
                setattr(self, name, kwargs[name])
            elif hasattr(self.__class__, name):
                setattr(self, name, getattr(self.__class__, name))
            else:
                raise TypeError(f"Missing required field: '{name}'")

    def __repr__(self):
        fields = {k: getattr(self, k) for k in self._get_all_annotations()}
        return f"{self.__class__.__name__}({fields})"
```

- **`Inputs`**: Declared on both `ProcessingTool` and `DataFrameTool`. Fields typed as `Annotated[Path, ImageSpec(...)]` or `ImageShared` represent data dependencies; scalar fields represent parameters. Default values are supported.
- **`Outputs`**: Required on `ProcessingTool`, optional on `DataFrameTool`. On `ProcessingTool`, path fields with `Template(...)` defaults are **output templates** resolved by the engine before execution (see [Section 7.1](#71-output-templating-engine)); fields without `Template(...)` defaults (e.g., `cell_count: int`) are computed values returned by the tool. Path outputs without a `Template(...)` default use the built-in default template. On `DataFrameTool`, `Outputs` enables construction-time validation of downstream column references. `DataFrameTool` may also declare `class Outputs(Passthrough): pass` to indicate that all input columns are preserved.

Both models use only standard-library types and `bioimageflow-core` types.

**Orchestrator-side validation:** The orchestrator (`bioimageflow` package) automatically builds Pydantic models from `IOModel` declarations for full validation during column resolution. This is transparent to tool authors:

```python
# bioimageflow/validation.py (orchestrator-only, has pydantic)
from pydantic import create_model

def build_pydantic_model(tool_model_cls):
    """Convert a IOModel declaration into a Pydantic model for validation."""
    fields = {}
    for name, annotation in tool_model_cls._get_all_annotations().items():
        default = getattr(tool_model_cls, name, ...)  # ... = required
        fields[name] = (annotation, default)
    return create_model(tool_model_cls.__name__, **fields)
```

#### GUIMeta — Field-Level Metadata for GUI Frontends

*Module: `bioimageflow_core.tool`*

`Inputs` and `Outputs` fields can carry optional `GUIMeta` annotations that provide hints to GUI frontends (e.g., node editors, property panels). `GUIMeta` is a frozen dataclass attached via `typing.Annotated`, following the same pattern as `ImageSpec`. For file-based image fields, use `Annotated[Path, ImageSpec(...), GUIMeta(...)]`. For shared-memory image fields, use `ImageShared(..., gui=GUIMeta(...))`.

```python
class Connectable(Enum):
    """Whether a tool input field can be bound to an upstream dataframe column."""
    NEVER = "never"            # No input pin, no toggle — impossible to connect
    NOT_BY_DEFAULT = "not_by_default"  # Pin hidden by default; checkbox reveals it
    BY_DEFAULT = "by_default"  # Pin visible by default; checkbox can hide it

class PathPicker(Enum):
    """Which filesystem values a GUI path picker should offer."""
    FILE = "file"
    FOLDER = "folder"
    BOTH = "both"

@dataclass(frozen=True)
class GUIMeta:
    """
    GUI hints for an Inputs or Outputs field.
    Attached via Annotated — invisible to runtime logic, read by frontends.
    """
    display_name: str | None = None   # Human-readable label shown next to the field
    description: str | None = None    # Longer help text / tooltip
    connectable: Connectable = Connectable.NOT_BY_DEFAULT  # Pin visibility / connectability (Inputs only)
    min: float | int | None = None   # Minimum value (numeric fields)
    max: float | int | None = None   # Maximum value (numeric fields)
    step: float | int | None = None  # Step increment (numeric fields)
    group: str | None = None   # Logical group for tab/section display (e.g. "general", "advanced", "gpu")
    path_picker: PathPicker | None = None  # Path input picker mode; GUI-only
```

**Display text and description:**
- `display_name` — the short, human-readable label a GUI shows next to the widget (e.g. `"Cell diameter"` instead of the raw field name `diameter`). If `None`, frontends should fall back to the field name, typically prettified (snake-case → Title Case).
- `description` — a longer explanation intended for tooltips or inline help panels. Use it to describe *what* the field means, *why* a user would change it, and any units or valid ranges that are not obvious from `min` / `max` / `step`.

Both fields are purely cosmetic hints — the runtime never reads them.

**Connectable states (Inputs only):**
- `Connectable.NEVER` — the field can never be wired to an upstream column. No pin, no toggle. Use for source configuration fields (e.g. file path, glob pattern) or structural settings that never vary per-row.
- `Connectable.NOT_BY_DEFAULT` — the field is connectable, but the pin is hidden until the user enables it via a checkbox. Use for algorithm parameters (thresholds, model names) that are rarely column-bound but occasionally need to be.
- `Connectable.BY_DEFAULT` — the pin is visible out of the box. Use for data inputs (image paths, required columns) that almost always come from a dataframe column.

For `Outputs` fields, `connectable` is ignored (outputs always expose a pin).

**Path picker modes (path Inputs only):**
- `PathPicker.FILE` — offer file selection only.
- `PathPicker.FOLDER` — offer folder selection only.
- `PathPicker.BOTH` — offer both file and folder selection.

`path_picker=None` leaves selection to the GUI's type-based inference. The hint affects rendering only; it does not add filesystem validation and is ignored for non-path fields and Outputs.

**Defaults:** Fields without a `GUIMeta` annotation default to `connectable: Connectable.NOT_BY_DEFAULT` with no numeric constraints, no group, no path-picker override, and no display text or description. A GUI frontend inspects the `Annotated` metadata for each field; if no `GUIMeta` is found, it assumes the field is connectable but with the pin hidden by default, uses the field name as a fallback label, and provides no tooltip. Data input fields (image paths) should use explicit `GUIMeta(connectable=Connectable.BY_DEFAULT)` to make their pins visible.

**Usage:**

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import ProcessingTool, IOModel, ImageSpec, Semantic, Arguments, GUIMeta, Connectable, Template

class CellposeSegmenter(ProcessingTool):
    display_name = "Cellpose Segmenter"
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(
                display_name="Input image",
                description="Fluorescence or brightfield image to segment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        diameter: Annotated[float, GUIMeta(
            display_name="Cell diameter",
            description="Approximate diameter of cells in pixels. Set to 0 for auto-detection.",
            min=0.0, max=500.0, step=0.5, group="general",
        )] = 30.0
        model_type: Annotated[str, GUIMeta(
            display_name="Model",
            description="Cellpose pretrained model — e.g. 'cyto3', 'nuclei'.",
            group="general",
        )] = "cyto3"
        flow_threshold: Annotated[float, GUIMeta(
            display_name="Flow threshold",
            description="Maximum allowed flow error. Lower values reject more masks.",
            min=0.0, max=1.0, step=0.05, group="advanced",
        )] = 0.4
        use_gpu: Annotated[bool, GUIMeta(
            display_name="Use GPU",
            description="Run inference on GPU when available.",
            connectable=Connectable.NEVER, group="gpu",
        )] = True

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}),
            GUIMeta(
                display_name="Segmentation mask",
                description="Label image where each cell has a unique integer ID.",
            ),
        ] = Template("{input_image.stem}_mask_{row_index}.png")
        cell_count: Annotated[int, GUIMeta(
            display_name="Cell count",
            description="Number of cells detected in the image.",
        )]

    def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
        ...
```

In this example:
- `input_image` has `Connectable.BY_DEFAULT` with `display_name="Input image"` — the pin is visible and the GUI shows a friendly label and tooltip.
- `diameter` uses the default `NOT_BY_DEFAULT` with a slider range of 0–500, step 0.5, in the **general** tab.
- `model_type` uses the default `NOT_BY_DEFAULT`, in the **general** tab — rendered as a text field or dropdown, pin available via checkbox.
- `flow_threshold` is in the **advanced** tab — hidden from the main view, accessible via an "Advanced" tab.
- `use_gpu` is **never connectable** (`NEVER`), in the **gpu** tab — grouped with other GPU-related settings.
- Outputs (`mask`, `cell_count`) carry `display_name` and `description` so the GUI can label output pins and provide tooltips.

**Grouping behaviour:** A GUI frontend collects all fields sharing the same `group` value and displays them together (e.g. as tabs, collapsible sections, or accordion panels). Fields with `group=None` belong to an implicit default group. The ordering of groups is determined by first appearance in the `Inputs` declaration.

**Extracting GUIMeta:** Frontends and introspection utilities use `typing.get_args()` to retrieve `GUIMeta` from `Annotated` types:

```python
import typing

def get_gui_meta(annotation) -> GUIMeta | None:
    """Extract GUIMeta from an Annotated type, if present."""
    if typing.get_origin(annotation) is typing.Annotated:
        for arg in typing.get_args(annotation)[1:]:
            if isinstance(arg, GUIMeta):
                return arg
    return None
```

**Compatibility with image fields and ImageShared:** File-based image fields use `Annotated[Path, ImageSpec(...)]`, optionally with a `GUIMeta(...)` metadata entry. `ImageShared(...)` returns `Annotated[SharedArray, ImageSpec(...), GUIMeta(...)]` when `gui=` is supplied and always includes an implicit `formats={"memory"}` constraint. Data input fields (image paths, required columns) should use explicit `GUIMeta(connectable=Connectable.BY_DEFAULT)` to make their pins visible by default.

**Runtime behavior:** `GUIMeta` is purely declarative metadata — it has no effect on validation, execution, caching, or hashing. The orchestrator and worker environments ignore it entirely. It exists solely for GUI frontends to render appropriate widgets, labels, tooltips, path pickers, and port visibility.

### 3.6 Arguments and Column References

*Module: `bioimageflow_core.arguments` (Arguments), `bioimageflow.node` (ColumnRef)*

#### The `Arguments` Object

When the engine dispatches work to a tool, it constructs an `Arguments` namespace. For `ProcessingTool`, one `Arguments` per row containing all resolved input values and output template paths. For `DataFrameTool`, a single `Arguments` containing the tool's constant parameters. Fields declared as `Path` or `Annotated[Path, ...]` are absolute runtime paths by the time the tool receives them.

The tool accesses values via attribute access: `arguments.input_image`, `arguments.diameter`, `arguments.mask`.

```python
from difflib import get_close_matches as _get_close_matches

class Arguments:
    """
    Lightweight namespace for passing resolved values to tool methods.
    Constructed from a dict; supports attribute access.
    Provides helpful error messages on typos via __getattr__.
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        available = [k for k in self.__dict__ if not k.startswith('_')]
        close = _get_close_matches(name, available, n=3, cutoff=0.6)
        msg = f"Arguments has no field '{name}'."
        if close:
            msg += f" Did you mean: {', '.join(close)}?"
        else:
            msg += f" Available fields: {', '.join(sorted(available))}"
        raise AttributeError(msg)
```

#### Column References (`ColumnRef`)

`ColumnRef` is created by subscripting a Node: `node["column_name"]`. It binds a specific upstream column to a tool input field. `ColumnRef` is internal to the orchestrator — workflow developers create it implicitly via `node["col"]`, never importing it directly.

```python
@dataclass(frozen=True)
class ColumnRef:
    """References a specific column from a specific upstream node."""
    node: "Node"
    column: str
```

**Shorthand rule:** When a bare Node (not a ColumnRef) is passed as a keyword argument `field=node`, it is equivalent to `field=node["field"]` — the engine looks for a column with the same name as the input field. If no such column exists, a `ColumnNotFoundError` is raised at graph construction time with a clear message listing available columns.

### 3.7 Merge via DataFrameTool

When a tool needs data from multiple upstream sources, the DataFrames must be explicitly combined using a DataFrameTool node. There is no implicit merge mechanism on ProcessingTool — every multi-source combination is a visible step in the DAG.

**ProcessingTool** receives inputs from individual column references (`node["col"]`). When references come from multiple upstream nodes, the engine aligns values by index (see [Section 5.3](#53-dataframe-semantics)). The upstream nodes must share a common lineage — if they are from unrelated branches (e.g., two independent `load_images` calls), the engine raises `IndexAlignmentError` and the user must insert a merge DataFrameTool.

**Usage:**

```python
from bioimageflow_common_tools import CrossJoin, JoinOnColumn

# Combinatorial pairing
paired = CrossJoin()(set_a, set_b)
results = compare(image_a=paired["path_left"], image_b=paired["path_right"])

# Parameterized join on a specific column
merged = JoinOnColumn()(patients, scans, join_column="patient_id", how="left")
analysis = analyze(image=merged["scan_path"], age=merged["age"])
```

Custom merge strategies are simply DataFrameTool subclasses that override `merge_dataframes`. The signature is `(self, dfs: list[DataFrame], arguments: Arguments) -> DataFrame`.

### 3.8 Import Conventions

ProcessingTool dependencies (those specific to the tool, not in `bioimageflow-core`) are imported **inside** `process_row` / `process_batch`, not at module level. This prevents `ModuleNotFoundError` when the tool class is loaded in contexts where the tool's heavy dependencies are not installed.

For IDE support, use `TYPE_CHECKING`:
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import cellpose.models  # Visible to IDEs and type checkers, not imported at runtime
```

Imports from `bioimageflow-core` (e.g., `ImageSpec`, `Arguments`, `IOModel`) can be at module level since the package is always available and only carries worker-safe dependencies.

DataFrameTool definitions import from `bioimageflow` and run exclusively in the main process, so they have full access to Pandas and any main-process library at module level:
```python
from bioimageflow import DataFrameTool
from bioimageflow_core import IOModel, Arguments
import pandas as pd  # Available — DataFrameTool runs in main process only
```

### 3.9 Image I/O

*Module: `bioimageflow_core.io`*

Image I/O uses a **pluggable dispatch** pattern. The tool provides its own file reader/writer; `bioimageflow-core` handles the dispatch between file paths and shared memory references without depending on image IO libraries.

```python
from contextlib import contextmanager
from collections.abc import Iterator

@contextmanager
def load_image(
    source: Path | str | SharedArray,
    *,
    file_reader: Callable[[Path], Any],
) -> Iterator[Any]:
    """
    Dispatch between file and shared memory sources.

    - Path or str: delegates to file_reader (provided by the tool) and yields
      the loaded object.
    - SharedArray: attaches to the shared memory segment and yields a zero-copy
      numpy view. The shared memory handle is closed automatically when the
      context exits.
    """
    if isinstance(source, SharedArray):
        import numpy as np
        from multiprocessing.shared_memory import SharedMemory
        shm = SharedMemory(name=source.name)
        try:
            arr = np.ndarray(source.shape, dtype=source.dtype, buffer=shm.buf)
            yield arr
        finally:
            shm.close()
    else:
        yield file_reader(Path(source))

def save_image(
    destination: Path | str,
    data: Any,
    *,
    file_writer: Callable,
) -> None:
    """Save image data to disk using the provided writer."""
    file_writer(Path(destination), data)
```

**Usage in a ProcessingTool:**
```python
def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
    import imageio.v3 as iio
    from bioimageflow_core.io import load_image, save_image

    # The tool provides its own reader/writer — bioimageflow-core only dispatches
    with load_image(arguments.input_image, file_reader=iio.imread) as image:
        result = some_processing(image)
    save_image(arguments.mask, result, file_writer=iio.imwrite)

    return self.Outputs(mask=arguments.mask, cell_count=42)
```

Tools that do not need the Path/SharedArray dispatch can skip `load_image` entirely and call their own I/O libraries directly.

### 3.10 Tool Packaging and Versioning

*Module: `bioimageflow.tool_loader`*

Tools are distributed as standard Python packages. The package version participates in result-key material for caching (see [Section 6.1](#61-result-key)). When a tool's package version changes, cached results for that tool are automatically invalidated.
First-party BioImageFlow packages are released independently, so package references must use versions allowed by each distribution's declared compatibility requirements.

#### Package Structure Requirements

Tool packages **must use relative imports** for all intra-package references. This is critical for the versioned loading mechanism to work correctly:

```python
# Correct — relative imports
from .gaussian import GaussianSmooth
from .utils.filters import apply_filter

# Wrong — absolute imports break versioned loading
from simpleitk_tools.gaussian import GaussianSmooth
from simpleitk_tools.utils.filters import apply_filter
```

**Why:** When multiple versions are loaded, each lives under a scoped namespace (e.g., `simpleitk_tools__1_0_0`). Relative imports resolve within the correct scoped namespace. Absolute imports bypass the scoping and resolve to whichever version was loaded first (or to the canonical name if registered), silently mixing code from different versions.

This applies everywhere: `__init__.py`, tool modules, workflow factories, and utility modules.

#### Tool Store

Tool packages are installed in a **tool store** — a directory that holds versioned copies of each package. The default resolution order is `BIOIMAGEFLOW_TOOL_STORE`, then `BIOIMAGEFLOW_HOME/tool_packages`, then `~/.bioimageflow/tool_packages`. Multiple versions of the same package coexist as distinct directory trees:

```text
~/.bioimageflow/tool_packages/
  simpleitk_tools/
    1.0.0/
      simpleitk_tools/           # full Python package tree
        __init__.py
        gaussian.py
        base.py
        utils/
          __init__.py
          filters.py
    2.0.0/
      simpleitk_tools/
        __init__.py              # different code
        gaussian.py
        ...
```

Packages are installed via `<orchestrator-python> -m pip install --target <dir> simpleitk-tools==X.Y.Z`.
This tool-store operation is intentionally independent of the Wetlands 2 environment lifecycle.
Set `BIOIMAGEFLOW_TOOL_STORE` when a workflow needs an explicit shared or project-local store.

#### Versioned Loading

`load_versioned_package(package, version, store_path)` loads a tool package from the tool store into an **isolated namespace** in `sys.modules`. The package is scoped under a synthetic name (e.g., `simpleitk_tools__1_0_0`) so that two loads of the same package at different versions produce **distinct class objects** that share `bioimageflow-core` base classes (since those come from the orchestrator's own environment).

```python
from bioimageflow import load_versioned_package

v1 = load_versioned_package("simpleitk_tools", "1.0.0")
v2 = load_versioned_package("simpleitk_tools", "2.0.0")

# Two distinct class objects
assert v1.GaussianSmooth is not v2.GaussianSmooth

# Both are subclasses of ProcessingTool
assert issubclass(v1.GaussianSmooth, ProcessingTool)
assert issubclass(v2.GaussianSmooth, ProcessingTool)

# Both can coexist in the same workflow
with Workflow(storage_path="./results") as wf:
    smooth_v1_result = v1.GaussianSmooth()(input_image=raw["path"], sigma=1.0)
    new_result = v2.GaussianSmooth()(input_image=raw["path"], sigma=1.0)
    results = wf.compute(smooth_v1_result, new_result)
```

The loading mechanism:

1. Creates a top-level module entry in `sys.modules` under the scoped name with `submodule_search_locations` pointing at the versioned directory.
2. Installs a temporary meta-path import hook so that relative imports within the package (e.g., `from .gaussian import GaussianSmooth`) resolve to the versioned directory under the scoped namespace.
3. Executes the package's `__init__.py`, which triggers all `from .xxx import ...` chains.
4. Materializes public exports declared in `__all__` by resolving each name with `getattr(package_module, name)`. This supports package-level lazy exports implemented with `__getattr__`, as long as each public export can be imported in the orchestrator process.
5. Stamps every `BaseTool` subclass found in the loaded modules with metadata: `_bif_package`, `_bif_package_version`, `_bif_canonical_module`.

This works transparently for all tool types:

- **ProcessingTools**: Loaded as real subclasses with real `process_row`/`process_batch`. `inspect.getfile()` returns the versioned path. Wetlands dispatch works unchanged.
- **DataFrameTools**: Loaded as real subclasses with real `transform()`/`merge_dataframes()`. They execute in the main process as usual.
- **Workflow factories**: reusable workflow modules import versioned tools normally and return a fresh `Workflow`; workflows are not tool classes.

#### Version Metadata

Every tool class loaded from the tool store carries three attributes stamped by the loader:

| Attribute | Description |
|-----------|-------------|
| `_bif_package` | Package name (e.g., `"simpleitk_tools"`) |
| `_bif_package_version` | Package version (e.g., `"1.0.0"`) |
| `_bif_canonical_module` | The canonical (unscoped) module path (e.g., `"simpleitk_tools.gaussian"`) |

The `get_tool_package_info(tool)` helper returns `(package, version, canonical_module)` for any tool class or instance. For tools not loaded from the tool store, it returns `(None, None, tool.__module__)`.

The `get_tool_version()` function (used by the cache system) checks `_bif_package_version` first, falling back to `importlib.metadata` and file mtime for non-versioned tools.

#### Resolving Tool Classes

`resolve_tool_class(package, version, canonical_module, class_name)` finds a tool class within a loaded versioned package. It maps the canonical module path (e.g., `simpleitk_tools.gaussian`) to the scoped module (`simpleitk_tools__1_0_0.gaussian`) and retrieves the class by name. This is used by `Workflow.load()` to reconstruct nodes from serialized JSON.

#### Cleanup

`unload_versioned_package(package, version)` removes all `sys.modules` entries for a scoped package version, including any canonical name aliases created by `require_tool_packages`. It also removes the corresponding `sys.path` entry for transitive dependencies. After unloading, `load_versioned_package` for the same version loads fresh module and class objects.

#### Transitive Dependencies

When a versioned package is loaded, its version directory (e.g., `~/.bioimageflow/tool_packages/simpleitk_tools/1.0.0/`) is prepended to `sys.path`. This makes third-party libraries installed alongside the package (via `uv pip install --target`) importable by main-process code — important for `DataFrameTool` classes or `__init__.py` files that import non-standard libraries at module level. The entry is removed on `unload_versioned_package`.

#### Shareable Workflow Scripts (PEP 723)

Workflow scripts can declare their tool dependencies using [PEP 723](https://peps.python.org/pep-0723/) inline script metadata. This makes scripts fully self-contained and shareable — a recipient can run the file directly, and missing packages are installed automatically.

```python
# /// script
# dependencies = [
#   "simpleitk-tools==1.0.0",
#   "cellpose-tools==2.3.1",
# ]
# ///

from bioimageflow import Workflow, require_tool_packages, configure_wetlands
# Optionally configure wetlands
configure_wetlands(root="./wetlands")

# Parse PEP 723, install missing packages into tool store, load all
require_tool_packages(__file__)

# Normal imports work — no scoped names needed
from simpleitk_tools import GaussianSmooth
from cellpose_tools import CellposeSegmenter

with Workflow(storage_path="./results") as wf:
    raw = FileLoader()(path="./data")
    smoothed = GaussianSmooth()(input_image=raw["path"], sigma=2.0)
    cells = CellposeSegmenter()(input_image=smoothed["output"])
    wf.compute(cells)
```

`require_tool_packages(script_path, *, store_path=None, auto_install=True)` does the following:

1. **Parses PEP 723 metadata** from the given script file. Extracts the `dependencies` list from the `# /// script` TOML block.
2. **Requires exact version pins** (`==`). Flexible specifiers like `>=1.0` or `~=1.0` are rejected with a `ValueError` — reproducibility demands pinned versions.
3. **Normalizes package names**: converts PyPI names to Python module names (`simpleitk-tools` → `simpleitk_tools`).
4. **Auto-installs missing packages** into the tool store via the orchestrator interpreter's `python -m pip install --target`. Set `auto_install=False` to raise `FileNotFoundError` instead.
5. **Loads each package** via `load_versioned_package()`.
6. **Registers canonical names** in `sys.modules`: copies every scoped entry (e.g., `simpleitk_tools__1_0_0.gaussian`) to its canonical equivalent (`simpleitk_tools.gaussian`). This enables standard `from simpleitk_tools import GaussianSmooth` syntax.

This is safe because PEP 723 declares exactly one version per package — there is no ambiguity about which version to bind to the canonical name. For the advanced case of loading two versions of the same package simultaneously, use `load_versioned_package()` directly.

#### Auto-Install on JSON Load

`Workflow.load()` also auto-installs missing versioned packages.
When a serialized workflow references `tool_package` and `tool_package_version`, the loader checks the tool store and installs through the orchestrator interpreter if the package is absent.
This means both `.py` scripts and `.json` workflow files are self-resolving when that environment provides `pip`.

### 3.11 Tool Registry

`ToolRegistry` is the public, stateful index for tool classes loaded from versioned packages and for custom tools embedded in a workflow export. It is the surface GUIs and other host applications should use to enumerate and resolve tools — it wraps `load_versioned_package`, `resolve_tool_class`, workflow custom-tool discovery, and the schema serializers behind a single object so consumers do not have to rebuild metadata serialization or package-resolution layers themselves.

The registry deliberately separates **install** (slow, network-bound) from **register** (fast, in-process index lookup):

```python
from bioimageflow import ToolRegistry, ToolMetadata

reg = ToolRegistry()                                 # uses default tool store path
reg.install_package("cellpose_tools", "2.3.1")       # network: installs via host Python
metas: list[ToolMetadata] = reg.register_package(    # fast: loads + indexes already-installed pkg
    "cellpose_tools", "2.3.1"
)

reg.get_class("CellposeSegmenter")                   # type | None
reg.get_metadata("CellposeSegmenter")                # ToolMetadata | None
reg.list_tools()                                     # list[ToolMetadata] in registration order
reg.forget("CellposeSegmenter")                      # drop from index; no-op if absent
```

`register_package` raises `FileNotFoundError` when the package is not present in the store — it never reaches for the network. Callers that validate on every keystroke must call `register_package` on hot paths and `install_package` only from explicit user actions.

For a specific workflow or platform workspace, GUIs must also register the
custom tools bundled with that project:

```python
metas = reg.register_workflow(workflow)      # live Workflow object
metas = reg.register_workflow(workflow_data) # exported workflow dict
```

`register_workflow` discovers only custom tools carried by that workflow export
or project context. It does not install or register package tools; call
`register_package` for package references. For exported dicts, discovery uses
the `custom_sources` archive table written by `Workflow.export()`.

`ToolMetadata` is a frozen dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `package` | `str` | Package import name (e.g. `"my_tools"`). |
| `version` | `str` | Pinned package version. |
| `module` | `str` | Canonical module path (not the scoped `__1_0_0` variant). |
| `class_name` | `str` | The tool class name as written in source. |
| `inputs_schema` | `dict[str, Any]` | Output of `serialize_input_schema(cls)`. |
| `outputs_schema` | `dict[str, Any]` | Output of `serialize_output_schema(cls)`. |
| `display_name` | `str` | The class's `display_name` attribute, or `class_name`. |
| `tags` | `tuple[str, ...]` | The class's `tags` attribute (empty tuple if none). |

The registry indexes `BaseTool` subclasses only; abstract base classes are excluded. Multiple versions of the same package can be registered because `resolve_tool_class` keys on the scoped module, not the class name alone.

### 3.12 Project-Local Custom Tools

A project may define custom `ProcessingTool` or `DataFrameTool` classes in a
project-local `tools/` package and reusable workflow factories alongside them. In the BioImageFlow
platform, the project root is the user's workspace, so workspace-owned custom
tools live under `workspace/tools/` and can be reused by any workflow in
`workspace/workflows/`. These tools do **not** need to be promoted to a
versioned tool package merely to make a workflow shareable.

Recommended layout:

```text
workspace/
  workflows/
    segmentation/
      workflow.json
      results/
  tools/
    download_images.py
    measure_spots.py
    utils.py
    data/
      small_runtime_asset.json
  tests/
    test_tools.py
      tiny_input.csv
```

Rules:

- Tool classes live under `tools/` and workflows import them from concrete modules, for example `from tools.download_images import DownloadImages`.
- `tools/` may omit `__init__.py` on supported Python versions.
  If a workflow adds `tools/__init__.py` as a regular package marker, it must be empty or limited to a docstring.
  Do not use eager barrel exports such as `from .some_tool import SomeTool` in `tools/__init__.py`, because a worker importing one tool module executes the package initializer first and should not need dependencies for unrelated tools.
- Helper modules, package constants, and small runtime assets needed by custom tools may also live under `tools/`. Use relative imports inside `tools/` so both the main process and workers can import the bundled package.
- Custom tools must have tests. Minimum coverage is schema serialization / validation plus one small execution test per custom tool behavior. Add integration coverage for tools that touch the workflow graph, output templates, environments, or workflow boundaries.
- Committed test fixtures live under `tests/data/` and must be small. Generated outputs, caches, and downloaded files live under pytest temporary directories or the workflow `storage_path`.
- Platform-created workflows use `<workflow-directory>/results/`, for example `workspace/workflows/<workflow-id>/results/`.
- Runtime storage is supplied when materializing the workflow and never enters the workflow graph or portable archive.
- Static assets may be committed under `tools/data/` when they are small and intrinsic to the custom tool. Large binaries, trained models, downloaded datasets, and generated artifacts must be declared as dependencies, downloaded at runtime, or generated by the workflow.

Export behavior:

- `Workflow.export(path)` calls `to_dict(include_custom_tools=True)` and writes
  a top-level `custom_sources` list containing a bundle for each used
  project-local `tools/` directory.
- The bundle preserves relative paths under `tools/` and includes file hashes plus an overall bundle hash. Generated/cache files such as `__pycache__`, `.pyc`, `.pytest_cache`, and hidden temp files are excluded. Export fails for unexpectedly large files.
- Tool nodes reference an embedded `tools/` bundle with `source_module`; nested graphs share the same archive-level source table.
- `Workflow.load(path, storage_path=...)` validates the embedded bundle hash, materializes the `tools/` tree into a scoped temporary Python package, and resolves the class from that package before attempting package or normal import resolution.
- `ToolRegistry.register_workflow(workflow_or_data)` discovers project-local
  custom tools from either a live `Workflow` or an exported workflow dict, so GUI
  tool discovery for a selected workflow includes the relevant custom tools.

---

## 4. Workflow Definition and Graph Engine

### 4.1 Workflow Construction

Users build workflows by calling tools as functions. Each call returns a **Node** — a lazy promise of future computation. Nodes form a DAG implicitly through their data dependencies. The calling convention differs by tool type: `ProcessingTool` takes keyword arguments (column references, node shorthand, or constants); `DataFrameTool` takes positional arguments (upstream nodes) and keyword arguments (parameters).

```python
# --- Instantiate tools ---
load_images = FileLoader()
extract_metadata = ColumnRegex()           # DataFrameTool
filter_quality = FilterRows()              # DataFrameTool
segment = CellposeSegmenter()             # ProcessingTool
analyze = Stats()                          # ProcessingTool

# --- Build the graph (no computation happens here) ---

# 1. Source node
raw_images = load_images(path="./data")

# 2. DataFrameTool: extract metadata from filenames (positional upstream)
with_metadata = extract_metadata(
    raw_images,
    column_name="filename",
    regex=r"(?P<patient>\w+)_(?P<slice>\d+)"
)

# 3. DataFrameTool: filter rows (positional upstream)
good_images = filter_quality(with_metadata, column_name="quality", min=0.5)

# 4. ProcessingTool: segmentation (explicit column reference)
masks_30 = segment(input_image=good_images["path"], diameter=30)

# 5. Branching: reuse the same upstream with different params
masks_50 = segment(input_image=good_images["path"], diameter=50)

# 6. Downstream: reference columns from different ancestor nodes
results = analyze(image=good_images["path"], mask=masks_30["mask"])

# --- Execution ---
# Traces back: results -> masks_30, good_images -> ... -> raw_images
# masks_50 is NOT computed because results doesn't depend on it
final_df = results.compute()
```

**Compound patterns (init + compute):** By chaining a `DataFrameTool` before a `ProcessingTool`, users achieve the equivalent of Fractal's compound tasks — the DataFrameTool reshapes the DataFrame (deciding what to process and how), and the ProcessingTool processes each row in parallel:

```python
# DataFrameTool: pair each image with its reference (init phase)
prepare = PrepareRegistration()
paired = prepare(raw_images, acquisition=0)

# ProcessingTool: register each image to its reference (compute phase)
register = RegisterImage()
registered = register(input_image=paired["image_path"], reference=paired["reference_path"])
```

**ProcessingTool as source node (isolated file discovery):**

```python
class DicomLoader(ProcessingTool):
    """List DICOM files and extract metadata — requires pydicom, isolated from main process."""
    display_name = "DICOM Loader"
    environment = EnvironmentSpec(name="dicom", dependencies={"conda": ["pydicom=3.0.1"]})

    class Inputs(IOModel):
        directory: str

    class Outputs(IOModel):
        path: Path
        patient_id: str
        modality: str

    def process_row(self, arguments: Arguments) -> list[Outputs]:
        import pydicom
        from pathlib import Path
        results = []
        for f in Path(arguments.directory).glob("**/*.dcm"):
            ds = pydicom.dcmread(f, stop_before_pixels=True)
            results.append(self.Outputs(
                path=f, patient_id=ds.PatientID, modality=ds.Modality
            ))
        return results

# Used as a source node — no upstream references, only constants
dicoms = DicomLoader()(directory="/data/hospital/")
segmented = segment(input_image=dicoms["path"])
```

**Multi-source workflow with explicit merge:**

```python
from bioimageflow_common_tools import CrossJoin, JoinOnColumn

mri = load_images(path="./mri/")
ct = load_images(path="./ct/")
patients = load_csv(path="patients.csv")

# Extract patient IDs from filenames
mri_meta = column_regex(mri, column_name="filename", regex=r"(?P<patient_id>\w+)_mri")
ct_meta = column_regex(ct, column_name="filename", regex=r"(?P<patient_id>\w+)_ct")

# Parameterized merge: join on patient_id
paired = JoinOnColumn()(mri_meta, ct_meta, join_column="patient_id", suffixes=("_mri", "_ct"))

# Enrich with patient metadata
enriched = JoinOnColumn()(paired, patients, join_column="patient_id", how="left")

# Process each pair — explicit column references, no ambiguity
registered = register(
    fixed=enriched["path_mri"],
    moving=enriched["path_ct"],
    patient_age=enriched["age"]
)
```

### 4.2 Nodes and Edges

- **Nodes** wrap a tool instance and its configuration (explicit arguments). Each node has a unique **node name** — either user-provided via `name=` in `__call__` or auto-generated from the tool class name and a counter (e.g., `CellposeSegmenter_1`, `CellposeSegmenter_2`). Node names must be unique within a Workflow; a tool class can be instantiated by multiple nodes.
- **Edges** represent data dependency: an edge from Node A to Node B means Node B references columns from Node A (via `ColumnRef`) or receives Node A's output DataFrame (via positional argument to a DataFrameTool).
- **`ColumnRef`** is created by subscripting a Node: `node["col"]`. It records the upstream node and column name. The engine validates column existence at construction time using `Node.get_output_schema()` (see §3.5), which covers both static `Outputs` and dynamic-but-resolvable schemas — `Generate(column_name="x")["x"]` validates immediately, and a fully-configured merge tool (e.g. `CrossJoin(Files(...), Generate(column_name="sensitivity", ...))`) validates the union of upstream column names. Validation is deferred to execution time only when the schema cannot be resolved (e.g. an upstream merge whose own upstream is unresolvable).
- The graph must remain a DAG. Cycles are detected synchronously inside `__call__()` when the edge is created, providing instant feedback in scripts and notebooks.
- **Source nodes** are simply nodes with no upstream data dependencies — they are not a separate tool type or code path. Both tool types can act as source nodes:
  - A **DataFrameTool** with no positional arguments receives an empty `dfs` list in `merge_dataframes` and produces the initial DataFrame (e.g., by listing files in a directory).
  - A **ProcessingTool** with no `ColumnRef` or `Node` arguments (only constants or defaults) is executed through the same code path as any other ProcessingTool. With no column bindings, the engine uses a single-row index (`["0"]`), builds arguments from constants and defaults only, and dispatches to `process_row`/`process_batch` as usual. This is useful when listing or loading files requires specialized libraries (e.g., reading HDF5 headers, DICOM metadata, OME-TIFF pyramids) that should not pollute the main process.

**Wire-format edge entries.** In `Workflow.to_dict() / from_dict()` every edge has an opaque stable `id` and one explicit variant.
A `column` edge carries `source_node`, `source_output`, `target_node`, and `target_input`.
A `dataframe` edge carries `source_node`, `target_node`, and exactly one of `target_position` or `target_input`.
The `id` round-trips unchanged and is copied onto matching `ValidationError` records; DataFrame positions are represented directly and require no sentinel field value.

### 4.3 The `Workflow` Object

`Workflow(storage_path=..., name="workflow", display_name=None, ...)` owns definition metadata, its immediate node map, one public interface, and root execution configuration.
`display_name` defaults to `name`.
Structural workflow and node names are non-empty and cannot contain `/`, which separates scoped execution paths.

All public materialization APIs require one concrete runtime storage root:

```python
Workflow(storage_path, ...)
Workflow.from_python(path_or_module, *, storage_path)
Workflow.from_dict(data, *, storage_path, ...)
Workflow.load(path, *, storage_path)
Workflow.import_archive(path, destination, *, storage_path)
```

`storage_path` always means the workflow's runtime root for cache records, provenance, run views, transient workspaces, and materialized outputs.
It is not an override, has no implicit fallback, and is never serialized into a graph or archive.
`Workflow.import_archive()` keeps the persistent extraction `destination` separate from runtime `storage_path`; the platform convention is to use `<destination>/results/`.

Ordinary ad hoc graphs still register tools through the workflow context manager.
Reusable definitions additionally declare symbolic inputs and published outputs:

```python
workflow = Workflow(
    storage_path="./results",
    name="segment",
    display_name="Segment",
)
with workflow:
    image = workflow.input("image", ImagePath, id="input-image")
    masks = Segment()(input_image=image, name="segment")
    workflow.output("mask", masks["mask"], id="output-mask")
```

Calling this object in another active workflow returns `WorkflowNode`; root callers use `compute(inputs={...})`.
The `engine` preference is one of `direct`, `wetlands`, or `parsl` and is serialized as a string.
Live engines, Parsl Config/DFK objects, executor bindings, routes, task policy, credentials, and execution contexts are runtime-only and never enter graph or archive data.
An explicit engine passed to `compute()` or `compute_steps()` has precedence over the stored preference.
`to_dict()` emits the recursive graph.
`to_dict(include_custom_tools=True)` emits the portable archive envelope when custom sources exist.
`from_dict(data, storage_path=..., validate_only=True, partial=True)` retains its diagnostic tuple form but parses only schema version 1.

### 4.4 Progress Monitoring

Workflows provide a callback-based progress mechanism for monitoring long-running executions:

```python
from bioimageflow import Workflow, ProgressEvent

def on_progress(event: ProgressEvent):
    print(f"[{event.node_name}] {event.status} — row {event.row}/{event.total_rows}")

workflow = Workflow(storage_path="./results", on_progress=on_progress)
# ... build graph ...
results.compute()
```

`ProgressEvent` reports:

| Field         | Type            | Description                                            |
|--------------|-----------------|--------------------------------------------------------|
| `node_name`   | `str`           | Name of the node being executed                        |
| `status`      | `str`           | One of: `"started"`, `"row_progress"`, `"row_complete"`, `"completed"`, `"cached"`, `"failed"`, `"cancelled"` |
| `result_key`  | `str \| None`   | V1 result key when the event is tied to a cacheable node result |
| `record_id`   | `str \| None`   | Selected record ID for `"completed"` and `"cached"` events |
| `row`         | `int`           | Current row index (for `row_complete` and `row_progress`) |
| `total_rows`  | `int`           | Total number of rows for this node                     |
| `message`     | `str \| None`   | Sub-row progress message from `RemoteTaskHandle.update()` |
| `current`     | `int \| None`   | Sub-row progress current value                         |
| `maximum`     | `int \| None`   | Sub-row progress maximum value                         |
| `timestamp`   | `float`         | Unix timestamp                                         |
| `diagnostic`  | `NodeFailureDiagnostic \| None` | Structured, sanitized failed-node diagnostic available to attached callbacks. |

`result_key` and `record_id` are cache identities only.
Diagnostic signatures are separate debug values and are not cache identities.
For a cache miss, `"started"` may include `result_key` while `record_id` remains `None` until the result is published and selected.
For non-reusable execution, both identities remain `None`.
Row/chunk `row_complete` events are emitted once per row in aligned position order even when remote futures finish out of order; whole-node batch emits no row-complete events.

When using branch-level parallelism, progress events from concurrent nodes may interleave. The engine serializes all `on_progress` callback invocations via an internal lock, so the callback does not need to be thread-safe.

### 4.5 Environment Configuration

`Workflow.get_environment()` provides access to per-environment launch configuration. It accepts a tool instance, an `EnvironmentSpec`, or an environment name string.
This configuration is Wetlands-only.
Parsl uses executor bindings and rejects `max_workers`, `worker_env`, or `worker_timeout` as Parsl routing or capacity settings.
Wetlands 2 supports `max_workers` and `worker_timeout`; assigning the retained migration-only `worker_env` field fails before provisioning.

```python
wf = Workflow(storage_path="./results", max_workers=4)

segmenter = CellposeSegmenter()
wf.get_environment(segmenter).max_workers = 2
```

Multiple calls with tools sharing the same environment return the same `WorkflowEnvironment` object. Configuration set via one tool applies to all tools in that environment.

**`WorkflowEnvironment`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | — | Environment name (read-only) |
| `spec` | `EnvironmentSpec` | — | The environment specification (read-only) |
| `max_workers` | `int` | `0` | Number of worker processes. `0` = use `Workflow.max_workers`. |
| `worker_env` | `Callable[[int], dict] \| None` | `None` | Retained only to detect Wetlands 1 configurations and raise a migration error. |
| `worker_timeout` | `float \| None` | `None` | Inactivity timeout in seconds. When set, Wetlands' health monitor marks the active task as `FAILED` and replaces the worker if it sends no IPC message within this duration — useful for catching native-code deadlocks or segfaults that don't close the pipe. The engine adds its own safety timeout of `max(worker_timeout * 1.5, worker_timeout + 60)` to `task.wait_for()`; if that fires, `WorkerTimeoutError` is raised. `None` = no timeout. |

#### 4.5.1 Resource Lifetime and Ownership

The public `ResourceLifetime` string enum and `resource_lifetime` parameter define engine resource ownership:

| Value | Execution completion, failure, or cancellation | `engine.close()` | Ownership |
|-------|-------------------------------------------------|------------------|-----------|
| `"execution"` | Drains work and cleans engine-created resources. | Idempotent cleanup. | One execution. |
| `"engine"` | Drains work and retains engine-created resources. | Drains active work and cleans owned resources. | Engine session. |
| `"external"` | Drains only work submitted by this engine and leaves injected resources running. | Leaves caller resources running. | Caller. |

Direct execution accepts only the default no-resource `"execution"` policy.
Wetlands applies the policy to `WetlandsEnvManager` ownership.
Parsl applies it to DFK and executor ownership.
An injected Wetlands manager or Parsl DFK requires `"external"`, and `"external"` requires the corresponding injected resource.

Every engine exposes idempotent `close()`, `__enter__`, and `__exit__`; executing a closed engine raises `RuntimeError`.
Closing or exhausting a steps generator applies its cleanup policy after submitted work drains.

`Workflow.create_engine()` is the only engine factory:

```python
workflow.create_engine(
    *,
    resource_lifetime="execution",
    env_manager=None,
    parsl_config=None,
    dfk=None,
    executor_bindings=None,
    parsl_node_routes=None,
    parsl_environment_routes=None,
    parsl_shared_runtime_root=None,
    parsl_execution="workflow",
    parsl_task_policy=None,
)
```

Direct rejects `env_manager`, non-default lifetime values, and every Parsl argument.
Wetlands accepts its manager and lifetime arguments and rejects every Parsl argument.
Parsl rejects `env_manager`, requires bindings and exactly one of Config or DFK, and forwards only Parsl runtime values.
Bare `Workflow(storage_path="./results", engine="parsl").compute(...)` has no implicit cluster configuration and fails before graph execution with an explicit `ParslEngine` construction example.

`WetlandsEnvManager` provides these public, thread-safe lifecycle methods:

- `stop(env_name) -> bool` stops one tracked environment and returns whether it was running.
- `is_running(env_name) -> bool` reports whether the adapter tracks that name as launched.
- `running_environments() -> tuple[str, ...]` returns sorted tracked environment names.
- `shutdown_all() -> None` stops every tracked environment and is idempotent.

Manager status is lifecycle introspection, not a worker health probe.
Applications must not inspect or mutate `_envs` or `_launch_configs`.

A retained worker process preserves imported libraries, CUDA process state, module registries, and the cached `ProcessingTool` instance.
It does not cache objects that tool code reconstructs inside `process_row` or `process_batch`.
The first-party `Cellpose3`, `CellposeSAM`, and `StarDistSegmenter` tools therefore lazily cache one model on their worker-side tool instance.
Cellpose caches by `model_type`, StarDist caches by `model_name`, and inference-only arguments do not invalidate the model.
Changing the model key replaces the single cached reference, while `clear_model_cache()` provides explicit invalidation without accumulating an unbounded set of GPU models.
`clear_model_cache()` affects the tool instance in the current process; an orchestrator application invalidates remote worker caches by stopping that environment through `WetlandsEnvManager.stop()` or by closing its owning engine.
Other heavy tools remain responsible for applying the same pattern to objects they construct inside their processing methods.

#### 4.5.2 Parsl Engine Configuration

Parsl is installed through the bounded `bioimageflow[parsl]` extra.
Ordinary package import, non-Parsl workflow construction, serialization, validation, planning, and import of public Parsl value types do not import the external Parsl package.
The package is loaded only when a Parsl operation requires it.

The canonical attached API is:

```python
engine = ParslEngine(
    parsl_config=config,
    executor_bindings=bindings,
    resource_lifetime="engine",
)

with engine:
    result = workflow.compute(inputs=inputs, engine=engine)
```

`ParslEngine` accepts exactly one of `parsl_config` and `dfk`, required executor bindings, optional scoped-node and environment-identity routes, an optional absolute shared runtime root, `execution="workflow" | "parallel" | "sequential"`, `storage_mode="shared_fs"`, an optional `ParslTaskPolicy`, and `resource_lifetime`.
An injected DFK requires `resource_lifetime="external"`.
`storage_mode="staged"` is recognized and rejected until a separate staging contract exists.

`ParslTaskPolicy`, `WorkerSlotCapacity`, `ExecutorCapabilities`, `WorkerEnvironmentAttestation`, and `ExecutorBinding` are strict immutable JSON-safe values.
`ParslTaskPolicy(row_chunk_size=1, max_in_flight=32)` requires two positive integers.
`row_chunk_size` groups consecutive row positions into one task, while `max_in_flight` bounds all unfinished Parsl futures and may be lowered for a node by `ResourceSpec.max_concurrent`.
Whole-node `process_batch()` remains one task regardless of `row_chunk_size`.
Parsl Config must use `retries=0` because BioImageFlow owns deterministic task failure selection and rejects opaque Parsl retries.
Live Config, DFK, executor/provider objects, routes, credentials, and secrets remain runtime-only.
`ParslTaskError` is the public structured remote-error subtype.

#### 4.5.3 Submitted Parsl Execution

`submit_workflow()` is the public separate-process launcher above `ParslEngine`.
It allocates a run ID and complete control directory before starting or describing an orchestrator:

```python
run = submit_workflow(
    workflow,
    inputs=inputs,
    parsl_config=ParslConfigRef(
        "my_application.parsl_config:build_config",
        {"queue": "analysis"},
        secret_refs={"credential": "BIF_CLUSTER_CREDENTIAL"},
    ),
    executor_bindings=bindings,
    node_routes=node_routes,
    environment_routes=environment_routes,
    shared_runtime_root=shared_runtime_root,
    task_policy=ParslTaskPolicy(),
    launch=OrchestratorLaunchConfig(backend="local"),
)
```

`ParslConfigRef.factory` is an importable `module:callable` configuration factory.
Its keyword arguments are finite JSON-safe values and its optional secret references are opaque environment-variable names resolved by the orchestrator host.
Literal credentials must not be supplied, secret-looking field names are rejected, and arbitrary callables, pickle payloads, Config, DFK, executor, and provider objects are rejected.

The submitted workflow definition is exactly the recursive graph-v1 payload or archive-v1 envelope.
Runtime storage stays outside that definition: `workflow.storage_path` is normalized into launcher metadata and passed explicitly to `Workflow.from_dict(..., storage_path=...)` in the detached process.
Partial workflows, unresolved tools, invalid routes, unsupported launch backends, and unavailable secret references fail before allocation or process launch as applicable.

The invocation is either the root interface with serialized `inputs`, or ordered immediate node names in `targets`.
The two forms are mutually exclusive.
Typed constants preserve Python scalar, collection, and absolute Path values without pickle.
Root DataFrames are stored under the control directory and verified by transport and canonical logical digests before DFK acquisition.

`OrchestratorLaunchConfig(backend="local")` starts a separate Python process.
`backend="manual"` writes a reproducible shell-free command descriptor and remains prepared until that command is executed.
`PSIJLaunchConfig` submits exactly one Slurm, PBS, or LSF scheduler job for the orchestrator.
The cluster environment must contain `bioimageflow[parsl,psij]`, the selected site PSI/J executor plugin, the importable Parsl configuration factory, and every tool environment required by its workers.
It requires an explicit positive walltime and supports strict queue, project/account, core-count, absolute cluster work-directory, and hard-cancel values without native scheduler directives, scripts, environment strings, or live PSI/J objects.
Direct scheduler backend aliases and OAR are not supported.
The PSI/J launcher persists immutable submit intent before external submission and a correlated native-job receipt immediately afterward.
An intent without a receipt is never automatically resubmitted; it remains prepared with stable uncertain-submission metadata until explicitly cancelled.
Receipt-backed clients reconstruct the same executor and fixed shared PSI/J work directory to observe or cancel the exact native job after restart.
Launcher backends start only the orchestrator; Parsl providers allocate workers.

`WorkflowRun.open(storage_path, run_id)` reconnects without process-local state.
Its `status`, `refresh()`, `progress()`, `logs()`, `cancel()`, and `result()` methods read durable launcher artifacts.
Status uses `prepared`, `starting`, `running`, `finalizing`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, and `lost`.
Every state mutation is a guarded revision and claim-epoch compare-and-swap.

Cancellation of an active run commits durable `cancel_requested` state before signaling the workflow.
Successful return installation and cancellation race through the guarded `running -> finalizing` transition: cancellation wins before that transition, while finalizing is non-cancellable.
An optional local or PSI/J hard-cancellation grace may terminate the exact orchestrator process or scheduler job through a reconnected run handle; confirmed termination records `lost` because provider and writer cleanup cannot be proven.

A successful submitted run persists its exact public DataFrame or ordered mapping beneath `launcher/v1/runs/<run-id>/return/` before canonical and launcher success.
Record-backed path cells address exact immutable record IDs, external paths remain typed external references, and transient owned assets are copied into the return.
Historical result loading never consults `current.json`.

Laptop-to-cluster submission uses the system OpenSSH and SFTP clients plus the installed one-shot `bioimageflow-cluster-agent`.
`SSHSubmissionTransport` carries only a host alias or `user@host`, normalized absolute cluster staging root, safe absolute remote executable, and finite timeout.
OpenSSH retains normal user configuration, agent authentication, `ProxyJump`, and host-key verification; BioImageFlow supplies no credentials, arbitrary options, shell fragments, bootstrap commands, or host-key bypass.

Only a path-like root workflow input explicitly wrapped in `LocalUpload(Path(...))` reads laptop bytes.
Ordinary `Path` values remain explicit normalized absolute cluster paths without laptop filesystem probing, and path-looking strings remain strings.
Root DataFrames use the existing logical/Parquet transport, reject `LocalUpload` cells and relative typed `Path` cells, and preserve string cells.
The workflow's existing absolute cluster `storage_path` travels beside the graph/archive and remains the sole value passed to `Workflow.from_dict`.

The one-shot `bioimageflow.cluster.command.v1` protocol implements upload, submission, bounded observation, cancellation, and result preparation operations with exact JSON schemas and canonical UUID4 operation request IDs.
Operation receipts make equal retries idempotent and reject request-ID digest conflicts.
SFTP writes only beneath a server-issued partial upload path.
Commit verifies a complete canonical manifest, rejects unsafe or tampered trees, atomically marks the upload ready, and installs a read-only content-addressed object.
The staging root is disjoint from workflow storage and never changes launcher, cache, run-view, or output-view layouts.
Before launcher allocation, submit durably binds the request to one preallocated launcher run ID and then reuses the cluster-local Phase 1b allocation and PSI/J dispatch path.
Passing `transport=SSHSubmissionTransport(...)` to `submit_workflow()` requires `PSIJLaunchConfig` and returns `RemoteWorkflowRun`; omitting transport preserves the existing local `WorkflowRun` behavior and type.
`RemoteWorkflowRun.open(transport, storage_path, run_id)` reconnects without local control/view path claims and validates the cluster storage/run binding before exposing state.
Refresh delegates to the cluster-local `WorkflowRun.refresh()`, wait uses interruptible client polling with monotonic deadlines, and progress retains the server's global sequences.
`RemoteWorkflowRun.logs()` uses bounded internal pages, byte offsets, file identities, and truncation detection to assemble complete current stdout and stderr snapshots before replacement decoding.
Each call returns the complete currently available combined text; no byte cursor is exposed in the public API.
Cancellation is a receipt-backed retry-safe delegation to cluster-local `WorkflowRun.cancel()`.

Successful remote results are prepared as immutable content-addressed transport objects after validating the exact Phase 1b return.
The object contains exact Parquet frames, self-contained return assets, and only the immutable record assets named by typed return locators; it never consults `current.json` or copies launcher state, cache records, run views, or output views.
The laptop downloads into a unique sibling candidate, validates the final manifest, paths, file types, sizes, and SHA-256 digests, and atomically installs the requested destination.
Every component of the destination's parent path must already be a real directory and not a symlink.
An existing destination is accepted only when it is the exact verified bundle for the same run.
Only record-owned and return-owned cells are rebound to verified laptop-local assets; declared external cluster Paths remain cluster Paths, and SharedArray values receive fresh laptop-local backing.

### 4.6 Input Binding Logic (Graph Construction)

At graph construction time, the engine builds an **input binding plan** for each tool call. The binding rules differ by tool type, reflecting their different relationships with upstream data.

#### ProcessingTool Binding

All inputs are keyword arguments. Each must be one of:

1. **Column Reference (`node["col"]`):** Binds the input field to a specific column from a specific upstream node. Creates a dependency edge. The engine validates column existence and type compatibility (per [Section 2.4](#24-type-compatibility)) at construction time for upstream nodes with known output schemas (i.e., nodes whose tool declares `Outputs`). For DataFrameTool upstream nodes without `Outputs`, column validation is deferred to execution time.
2. **Node Shorthand (`node`):** Equivalent to `node["field_name"]` where `field_name` is the keyword argument name. Raises `ColumnNotFoundError` if the upstream node has no column with that name.
3. **Constant Value:** A literal value (not a Node or ColumnRef). Validated against the `Inputs` field type using Pydantic. Used as-is for all rows.
4. **Default Value:** If the `Inputs` field has a default and no argument was provided, use the default.
5. **Failure:** If no source is found for a required field, raise `BindingError` listing the missing field and available sources.

#### DataFrameTool Binding

Positional arguments are upstream nodes — their output DataFrames are passed to `merge_dataframes`. Keyword arguments are `Inputs` parameters (constants only, not column references).

Construction-time validation checks that keyword arguments match the tool's `Inputs` declaration (type-checked via Pydantic).

#### No Auto-Resolution

There is no implicit name-based or type-based column matching. Every column binding is explicit — the developer specifies exactly which column from which upstream node feeds each input field. This eliminates fragility from upstream schema changes and makes every data flow visible in the code.

#### Error Collection

When `Node.__init__` runs under `Workflow.capture_errors()`, `BindingError` / `ColumnNotFoundError` raised by the binding rules above are appended to the active capture buffer as `ValidationError` entries instead of raising, and the node is registered with best-effort partial bindings so subsequent nodes can still be wired. See [Section 4.3](#43-the-workflow-object) for the `capture_errors()` contract and the Validation Error Reference section for the `ValidationError` shape.

### 4.6 Enabling and Disabling Nodes

Nodes can be temporarily disabled so the engine skips them during execution. This is designed for GUI workflows where users want to iterate on part of a pipeline without executing expensive downstream nodes.

#### Node-Level API

Each node has an `enabled` attribute (default: `True`) and convenience methods:

```python
masks = segment(input_image=raw["path"])
masks.enabled          # True (default)
masks.disable()        # Sets enabled = False
masks.enable()         # Sets enabled = True
masks.enabled = False  # Direct assignment also works
```

#### Workflow-Level API

The Workflow provides `disable()` and `enable()` methods that accept node references or node names (strings). This is convenient for GUIs that know node names but may not hold Python references:

```python
wf.disable(masks)               # By reference
wf.disable("stub_segmenter_1")  # By name
wf.enable("stub_segmenter_1")   # Re-enable by name
wf.disable(masks, results)      # Multiple nodes at once
```

Passing an unknown name raises `KeyError`.

#### Execution Semantics

1. **Disabled nodes are not executed** — no cache lookup, no computation, no side effects.
2. **Implicit skip propagation** — any node whose upstream dependency chain includes a disabled node is also skipped (it cannot run without its inputs). This propagation is computed in O(V) after topological sort.
3. **Graph structure is preserved** — disabling a node does not alter edges, bindings, or registration. Re-enabling restores the original wiring.
4. **Caching is unaffected** — the `enabled` flag is not part of result-key material. Re-enabling a node with the same parameters and selected upstream records can hit the existing cache.
5. **Return value** — `compute()` returns results only for target nodes that were actually executed:
   - If all targets are disabled or have disabled upstreams, `DisabledNodeError` is raised.
   - If some targets are disabled in a multi-target call, only executed targets appear in the returned dict.

#### Step-by-Step Execution (`compute_steps`)

When using `compute_steps()`, skipped nodes are still yielded so the GUI can display them (e.g., grayed out). Each `NodeStep` exposes a `skipped` property:
`compute_steps()` remains a supported execution API and uses the same cache semantics as `compute()`: cache lookup uses result keys and selected `current.json` records, cache misses publish immutable records, and run views point at the selected records.

```python
for step in wf.compute_steps(results):
    if step.skipped:
        print(f"  [skipped] {step.node_name}")
        continue
    df = step.execute()
    print(f"  [done] {step.node_name}: {len(df)} rows")
```

Calling `execute()` on a skipped step raises `DisabledNodeError`.

#### Serialization

The `enabled` flag is persisted in the JSON export. When `enabled` is `False`, the node entry includes `"enabled": false`. Enabled nodes (the default) omit the key to keep the format clean:

```json
{
  "name": "segmenter_1",
  "type": "tool",
  "tool_module": "my_tools.segmenter",
  "tool_class": "Segmenter",
  "tool_package": "my_tools",
  "tool_package_version": "1.0.0",
  "constants": {"diameter": {"__type__": "float", "value": 30.0}},
  "enabled": false
}
```

`tool_module` stores the **canonical** module path (not the scoped `__1_0_0` variant). When `tool_package` and `tool_package_version` are present, `Workflow.load()` uses `load_versioned_package()` to load the package and `resolve_tool_class()` to find the class in the scoped namespace. Workflows should include package identity for package tools.

`Workflow.load(path, storage_path=...)` restores the flag: disabled nodes remain disabled in the loaded workflow.

### 4.7 WorkflowSession (Incremental Editing API)

`WorkflowSession` is a parallel, **dict-backed** model of a workflow designed for GUI clients that mutate the graph incrementally. The session is the canonical state — a `Workflow` is materialized on demand and cached across edits, with selective rebuilds triggered only by structural changes.

**Why a separate class?** `Workflow` builds nodes eagerly in `__init__`, with `_upstream_nodes` and column bindings wired at construction. Retrofitting incremental mutation onto that model would require invasive changes to `Node`. A dict-backed session, materialized to a `Workflow` only when needed, is both simpler and matches what GUIs actually want to send over the wire.

Both session constructors require the same runtime root and keep it outside the wire-format dictionary:

```python
WorkflowSession(data=None, *, storage_path, registry=None)
WorkflowSession.from_dict(data, *, storage_path, registry=None)
```

```python
from bioimageflow import WorkflowSession

results = workflow_directory / "results"
s = WorkflowSession(data, storage_path=results)
s = WorkflowSession.from_dict(data, storage_path=results)

# Mutations
s.add_node({"name": "load", "type": "tool", "tool_module": "...",
            "tool_class": "...", "tool_package": None,
            "tool_package_version": None, "constants": {...}})
s.remove_node("load")                  # also strips edges that touch it
s.add_edge({"type": "column", "id": "e1", "source_node": "load",
            "source_output": "path", "target_node": "seg",
            "target_input": "input_image"})
s.remove_edge("e1")                    # by edge id
s.set_constant("seg", "diameter", 30.0)
s.set_enabled("seg", False)

# Read-only views (deep copies)
s.nodes        # dict[str, dict] keyed by node name
s.edges        # list[dict]
s.errors       # list[ValidationError] from the last validate() (or empty)
s.failed_nodes # dict[str, ValidationError] from the last to_workflow() build

# Materialization
data = s.to_dict()                     # snapshot of the wire format
wf = s.to_workflow()                   # cached; rebuilt only on structural edits
errs = s.validate()                    # cached across non-structural edits
plan = s.plan()                        # refreshes storage-facing cache state
```

**Edit semantics.** Edits are split into two categories:

- **Structural edits** (`add_node`, `remove_node`, `add_edge`, `remove_edge`) invalidate the cached `Workflow` — the next `to_workflow()` call rebuilds.
- **Non-structural edits** (`set_constant`, `set_enabled`) update the cached `Workflow`'s node fields **in place**. A `set_constant` followed by `validate()` or `plan()` does not re-resolve any tool class — this is the contract that makes the session viable for keystroke-rate validation.

`to_workflow()` always uses `Workflow.from_dict(storage_path=s.storage_path, validate_only=True, partial=True, auto_install=False)`, so per-node failures surface in `wf.failed_nodes` rather than raising. Callers should treat `s.failed_nodes` and `s.is_partial` (via the cached workflow) as part of normal operation, not as exceptional state.

`WorkflowSession.plan()` does not reuse a stale storage snapshot.
It may reuse the materialized workflow object, but every call refreshes storage-facing cache state before planning.
This guarantees that cache status reflects the session's current `storage_path`, node enabled state, constants, graph structure, and external `current.json` changes even when the workflow object was materialized earlier.

**Round-trip identity.** `WorkflowSession(data, storage_path=results).to_dict()` preserves the wire format including edge `id` keys, constant envelopes, and the `enabled` flag (the latter is omitted when re-enabling, matching `Workflow.to_dict`'s clean form).
The session storage path remains runtime-only and is absent from the returned dictionary.

---

## 5. Execution

### 5.1 The Serialization Boundary

The system has an orchestrator context and optional isolated processing workers.
`ProcessingTool` spans the boundary for Wetlands and Parsl; direct execution calls the same tool contract locally.
`DataFrameTool` runs entirely in the orchestrator.

| Aspect          | Orchestrator (Main Process)                            | Isolated Processing Worker                             |
|----------------|--------------------------------------------------------|--------------------------------------------------------|
| **Role**        | Planning, scheduling, data management, DataFrameTool execution | Executing ProcessingTool logic                         |
| **Packages**    | `bioimageflow` + `bioimageflow-core` + Pandas + Pydantic + graph lib | `bioimageflow-core` + NumPy + tool dependencies        |
| **State**       | Holds the DataFrame, graph, cache                      | Runtime state allowed (e.g., cached model instances)   |
| **Data in/out** | Strict worker-safe task/result dictionaries            | Strict worker-safe task/result dictionaries            |

**Worker lifecycle contract:**
- State set on `self` during graph construction is not available in the worker.
- Worker-local runtime state is allowed and recommended for expensive resources (e.g., loaded GPU models cached in instance dictionaries).
- `process_row` / `process_batch` must be deterministic for the same `Arguments` and declared tool/runtime configuration.

### 5.2 Execution Lifecycle

When `node.compute()` is called:

1. **Graph Traversal:** Topological sort determines execution order. Only nodes in the dependency chain of the requested node are executed.

1b. **Disabled-Node Filtering:** After topological sort, the engine walks the ordered list and removes disabled nodes and any node whose upstream includes a disabled node (see [Section 4.6](#46-enabling-and-disabling-nodes)). This is O(V) since upstreams are already classified by the time each node is visited.

2. **Per-Node Execution** (in topological order, skipping filtered nodes). The engine dispatches to different paths depending on the tool type:

#### DataFrameTool Execution Path

   1. **Collect Upstream DataFrames:** Gather the output DataFrames from all positional upstream nodes.
   2. **Resolve Arguments:** Resolve `Inputs` parameters into a single `Arguments` object (all constants, validated via Pydantic). Path-typed values are converted to absolute runtime paths before `merge_dataframes()` or `transform()` is called.
   3. **Cache Check:** Resolve the optional reusable result key from node identity, resolved arguments, tool/environment identity, root DataFrame logical digests, and selected upstream provider/selector record references. If the key exists and `current.json` selects a valid reusable record, load it and skip to step 6.
   4. **Merge:** Call `tool.merge_dataframes(dfs, arguments)`. Default: inner join on index.
   5. **Transform:** Call `tool.transform(df, arguments)`. Returns a (potentially different) DataFrame. Default: identity (passthrough).
   6. **Caching:** When reusable, publish the result as an immutable cache record under `storage_path/cache/v1/` and update the run view from the selected record. When identity is unavailable, return the in-memory DataFrame without creating cache or view artifacts.

#### ProcessingTool Execution Path

   1. **Index Alignment:** Collect all upstream nodes referenced via column bindings. Compute the aligned index — the finest-grained index that is compatible with all upstream indices (see [Section 5.3](#53-dataframe-semantics)). If upstream indices are incompatible (no common lineage), raise `IndexAlignmentError`.
   2. **Value Resolution:** For each row in the aligned index, materialize input values from the column bindings. The orchestrator validates resolved values using Pydantic models built from the tool's `IOModel` declarations. Path-typed values are converted to absolute runtime paths in the orchestrator.
   3. **Output Templating:** Resolve output path templates for every row (see [Section 7.1](#71-output-templating-engine)). The orchestrator resolves paths before dispatch beneath the selected reusable-attempt or transient invocation `assets/` directory.
   4. **Cache Check:** Resolve the optional reusable result key from node identity, resolved arguments, tool/environment identity, and selected provider/selector record references. A valid selected record is loaded immediately. When the resolver returns `None`, execution uses the run-scoped transient path and creates no reusable cache state.
   5. **Execution Context:** Allocate a required invocation ID. Reusable work uses `cache/v1/results/<result-shard>/<result-key>/attempts/<attempt-id>/staging/`; non-reusable work uses `cache/v1/transient/runs/<run-id>/nodes/<node-key>/<invocation-id>/`. Build one `ExecutionContext` per input row and one batch context with shared `assets/`, `work/`, and `rows/` roots plus private row/batch directories.
   6. **Serialization:** Encode each remote call as strict `ProcessingTaskV1`. Every task has a task ID and invocation ID; `cache_attempt_id` is present only for reusable execution.
   7. **Backend Preparation:** Direct acquires no remote resource. Wetlands prepares the selected environment. Parsl completes route validation, archive materialization, DFK acquisition, and executor preflight before processing submission.
   8. **Dispatch:** If `process_batch` was overridden, call one whole-node batch operation. Otherwise, dispatch row calls or explicit row chunks. Wetlands and Parsl use the same core protocol, origin resolver, worker entry point, and worker-instance cache. Parsl submission is bounded and results are collected by aligned position rather than completion order.
   8b. **Output Validation:** The shared orchestrator validator reconstructs returned dictionaries in declared field order, rejects missing/extra fields and invalid types or paths, normalizes row and batch returns to `list[list[Outputs]]`, and enforces exact batch cardinality for direct, Wetlands, and Parsl.
   9. **DataFrame Construction:** Build the output DataFrame from the tool's results. The output contains **only** the columns declared in `Outputs` (no upstream columns are carried forward). The index is preserved from the aligned input index, with explosion for 1-to-N outputs (see Section 5.3). This DataFrame is the node's graph-level output and may be passed as a positional upstream input to a `DataFrameTool`; individual declared columns remain addressable through `ColumnRef` bindings. If a tool writes a resolved declared template output but returns zero dataframe rows, the dataframe remains empty; the asset is published through the record manifest, not by adding a sentinel row. If a table-only tool declares `zero_row_scalar_outputs`, each input row that returns zero dataframe rows publishes those scalar values as manifest-only `scalar_output` metadata; the values are included in record identity and run views but are not rehydrated into dataframe rows.
   10. **Caching:** Reusable work publishes the canonical logical dataframe and assets as an immutable record, selects through `first-valid`, and updates views from the selected record. Non-reusable work returns transient outputs without a record, pointer, latest view, or output projection.

#### ProcessingTool Backend Interaction (ProcessingTool Steps 6-10)

The immutable backend dispatch request contains resolved arguments and contexts, ordered aligned positions, the scoped node, active run context, required invocation identity, and optional reusable-attempt identity.
The scheduler owns cache lookup, publication, dataframe construction, progress, cancellation, and failure semantics around this request.

Remote backends encode the request as `ProcessingTaskV1` with schema `bioimageflow.processing_task.v1`.
The result uses `ProcessingTaskResultV1` with schema `bioimageflow.processing_result.v1`.
Both envelopes echo task ID, scoped node, invocation ID, optional cache attempt ID, retry number, mode, row positions, and row-index strings exactly.
Decoders reject unknown schemas or modes, missing or extra fields, malformed origins and paths, invalid scalar types, booleans in integer fields, duplicate positions, and mismatched result correlation.

`WorkerToolOriginV1` has exactly five variants: installed module, versioned module, shared module, source file, and materialized archive module.
Every remote backend uses the same strict origin resolver and processing entry point.
The worker caches tool instances by the SHA-256 digest of canonical JSON for the complete origin including class name, so equal class or module names from different origins cannot collide.

Direct invokes the shared output-normalization path locally.
Wetlands submits the core task entry point to its selected environment.
Parsl wraps the same core entry point in an explicit-DFK Python app bound to the selected executor label.
Backend routing metadata is outside the worker envelope.

### 5.3 DataFrame Semantics

- **No column carry-forward (ProcessingTool):** A ProcessingTool's output DataFrame contains **only** the columns declared in its `Outputs` class, plus the row index. Upstream columns are not carried forward. Downstream tools that need upstream data reference the originating node directly (e.g., `raw["path"]`). This makes output schemas deterministic — a node's output depends only on its own `Outputs` declaration, never on what happens upstream.
- **DataFrameTool output:** A DataFrameTool's output DataFrame is whatever `transform()` returns. The tool author decides which columns to include. This is where intentional carry-forward happens — tools like `FilterRows` naturally preserve all input columns, while tools like `CountLabelOverlaps` may produce entirely new schemas.
- **Transport:** Pandas DataFrames on the orchestrator side; `list[dict]` across the serialization boundary (ProcessingTool only).
- **Index:** The DataFrame index represents a unique identifier for each data item (e.g., image ID). It is preserved across nodes. DataFrameTools that intentionally change the data granularity (e.g., aggregation) may produce a new index.
- **Index alignment:** When a ProcessingTool references columns from multiple upstream nodes via ColumnRefs, the engine aligns values by index. If one upstream has a finer-grained index (due to explosion), the coarser index is expanded using parent-index lookup. For example, if `raw` has index `[0, 1, 2]` and `tiles` has index `[0::0, 0::1, 1::0, 1::1, 2::0, 2::1]`, referencing both aligns `raw[0]` with `tiles[0::0]` and `tiles[0::1]`, etc. If upstream indices have no common lineage (e.g., two independent `load_images` calls), the engine raises `IndexAlignmentError`. **Divergent sibling explosions** (same parent row exploded differently by two sibling nodes, e.g., Node A produces `0::0, 0::1` and Node B produces `0::0, 0::1, 0::2`) also raise `IndexAlignmentError` — the user must insert a merge DataFrameTool (e.g., `CrossJoin`) to explicitly define the combination.
- **Explosion and the `::` separator:** When `process_row` returns multiple outputs for a single row, the engine extends the index using `::` as the explosion separator: `"<parent>::0"`, `"<parent>::1"`, etc. Successive explosions nest naturally: `"img_001::0::2"` means "image img_001, first split, third tile." The `::` sequence is **reserved** — source nodes must not produce indices containing `::`. For `ProcessingTool` sources, the engine controls index assignment. For `DataFrameTool` sources, the engine validates the returned DataFrame's index at execution time.

  Lineage helpers are provided in `bioimageflow_core.arguments`:

  ```python
  def parse_index_lineage(index: str) -> list[str]:
      """Split an exploded index into its lineage components."""
      return index.split("::")

  def parent_index(index: str) -> str:
      """Return the parent index (strip last explosion level)."""
      parts = index.split("::")
      return "::".join(parts[:-1]) if len(parts) > 1 else index
  ```

---

## 6. Result Keys, Caching, and Provenance

### 6.1 Result Key

Before reusable cache lookup, each node derives a result key when every consumed upstream selected record is known.
The result key answers: "what computation is this, over which exact selected upstream records?"

Result-key material includes every value that can affect logical output and cache validity:

- BioImageFlow cache schema version.
- Workflow or workflow-fragment identity when relevant.
- Node definition identity.
- Tool identity and version.
- Environment dependency hash.
- Normalized parameters and statically declared input bindings or selectors.
- Selected upstream record references for every cacheable upstream value consumed by the node.
- Recursive provider/selector recipes for values published through workflow boundaries, resolved to selected real-provider records at runtime.
- Canonical logical digests for root DataFrame inputs.
- Declared external references consumed by the node.
- Development-mode source hash when development mode is enabled.
- Output contract version when output schema changes affect cache compatibility.

The result key must not include run IDs, invocation IDs, attempt IDs, task IDs, executor or scheduler metadata, wall-clock timestamps, hostnames, process IDs, absolute runtime paths, Parquet transport digests, shared-memory segment names, or human-facing run-view paths.
Compilation retains provider/selector recipes without substituting workflow-boundary diagnostic signatures.
Planning reports `PENDING_UPSTREAM` while a recipe depends on an unresolved selection.
At runtime, if any consumed value has no selected immutable provider record, the resolver returns `None`; that node and every dependent consumer execute without reusable lookup or publication unless a later explicit contract creates a selected record.

Result keys are stored under the canonical cache root:

```text
storage_path/cache/v1/results/<result-shard>/<result-key>/
```

`NodePlan.logical_signature` exposes a diagnostic value when callers need it.
Public cache APIs use result keys and selected record IDs.
Callers should treat `NodePlan.final_result_key` as the public opaque cache token and should not parse or persist the diagnostic logical signature.

### 6.2 Current Record Selection

Published node outputs are immutable records under a result-key directory.
`current.json` selects the reusable record for that result key.

If `current.json` is missing, cache lookup treats the result key as a miss.
If `current.json` is corrupt, points outside the result-key directory, points to a missing record, or points to a record whose manifest is invalid, cache lookup raises a cache corruption error.
Normal lookup must not repair corrupt `current.json` silently, must not choose a record by filesystem iteration, and must not replace an invalid current pointer during publication.

V1 uses one current-record policy: `first-valid`.
If two workers publish different records for the same result key, the first valid current record remains selected and later differing records are conflicts or alternates.
Downstream execution must consume the selected current record, not a non-current candidate produced by the same run.
Publication validates a complete private `records/.candidate.<attempt-id>.<nonce>/<record-id>/` tree before atomically renaming it into the immutable records namespace.
Current selection uses an atomic hard-link create-if-absent operation; a losing publisher validates and loads the selected winner before continuing downstream.

Each reusable record manifest stores `dataframe.logical_digest`, `dataframe.logical_schema`, and `dataframe.transport_digest`.
Logical fields determine record identity.
The transport digest validates the Parquet bytes but never changes record identity.
Loading validates both the bytes and the recomputed logical values.

### 6.3 Development Mode

In development mode (`workflow.compute(dev_mode=True)`), result-key material additionally includes a source hash of the tool class.
This auto-invalidates reusable records when tool code changes without requiring a version bump.
Development mode is intended for iteration; production workflows should rely on compatible declared dependencies and explicit tool package versions for reproducibility.

### 6.4 Limitations

- **Path-based external references:** V1 external reference identity is path-based, not content-based. If an input file is modified without changing its path, the cache may report a false hit. Users can manually invalidate affected nodes when needed.
- **Transitive dependency changes:** The environment dependency hash catches declared version spec changes, such as `cellpose==3.0` to `cellpose==4.0`. If a dependency changes without a changed declared version, bump the tool package version or use development mode to force re-execution.

### 6.5 Pre-execution Planning

`Workflow.plan()` exposes cache status and selected-record information without executing nodes.
Callers that need to report cache state should use `plan()` rather than reimplementing result-key composition.

```python
from bioimageflow import NodePlan, NodePlanStatus
plan: dict[str, NodePlan] = workflow.plan(dev_mode=False)
for name, entry in plan.items():
    assert isinstance(entry, NodePlan)
    # entry.node_name, entry.final_result_key, entry.selected_record_id, entry.status, entry.upstream, entry.logical_signature
    # entry.cached / entry.skipped: boolean shortcuts derived from status
```

`NodePlan` is a frozen dataclass with fields `node_name`, `final_result_key`, `selected_record_id`, `status` (a `NodePlanStatus`), `upstream` (tuple of upstream scoped names), `pending_upstreams` (tuple of upstream scoped names whose selected records are not known yet), and `logical_signature` (a diagnostic value, not a cache key).
The `cached` and `skipped` booleans are read-only shortcuts (`cached == status is CACHED`, `skipped == status is SKIPPED`).
`NodePlanStatus` values:

| Status | Meaning |
|--------|---------|
| `CACHED` | `current.json` selects a valid reusable record for the final result key; `compute()` would short-circuit if it consumes the same upstream record references. |
| `PRIOR_SELECTION_MISS` | The planned final result key has no selected current record, but the same node has another selected current record from prior result-key material. |
| `UNEXECUTED` | No reusable record exists yet for this node/result lineage. |
| `SKIPPED` | Node is disabled, or its upstream chain contains a disabled node. `final_result_key` and `selected_record_id` are `None`. |
| `PENDING_UPSTREAM` | At least one consumed upstream selected record is not known until that upstream executes. `final_result_key` is `None`. |

Nested workflow tools appear under scoped names `"workflow_node/internal_name"`.
The outer workflow-node entry's status is `CACHED` only when every internal node is `CACHED`, otherwise `PENDING_UPSTREAM` or `UNEXECUTED` according to the internal state.
`plan()` never launches a Wetlands environment.
It raises `CycleInWorkflowError` (a `ValueError` subclass exposing `.nodes: list[str]`) on a cyclic graph; call `workflow.validate()` first if a cycle is possible.

---

## 6.6 Validation Error Reference

`ValidationError` is a frozen dataclass produced by:

- `Workflow.capture_errors()` — for construction-time errors when the context is active.
- `Workflow.from_dict(data, storage_path=..., partial=True)` — for tool-resolution and per-node construction failures during deserialization (also accessible via `wf.errors` and `wf.failed_nodes` after the call).
- `Workflow.validate()` — for post-construction checks.
- `WorkflowSession.validate()` — same as `Workflow.validate()` but cached across non-structural session edits.

```python
from bioimageflow import ValidationError, ValidationErrorKind

@dataclass(frozen=True)
class ValidationError:
    kind: ValidationErrorKind
    message: str
    node: str | None = None
    field: str | None = None
    edge: tuple[str, str, str] | None = None        # (from_node, to_node, field)
    edge_id: str | None = None                      # opaque GUI-supplied id (see §4.2)
    path: tuple[str, ...] = ()                      # nested workflow scope, root → leaf
```

`edge_id` carries the optional `id` value that GUIs attach to edges in the wire format (see [§4.2 / Wire format](#42-nodes-and-edges)). When an error is raised against an edge that has an `id`, the library copies that id onto the `ValidationError`. This is the disambiguator for cases like positional args, where multiple edges share the same `(from, to, field)` triple by construction. `edge_id` is also part of the deduplication key inside `validate()` — two errors that differ only by their `edge_id` are reported as distinct.

**The library never raises `ValidationError`.** It raises the existing domain exceptions (`BindingError`, `ColumnNotFoundError`, `IndexAlignmentError`, `SourceToolUpstreamError`, `ValueError` for duplicate names, `CycleInWorkflowError` for cycles in `plan()`) unless an error-collector is active.

**`ValidationErrorKind` values and origins:**

| Kind | Produced when |
|------|----------------|
| `cycle` | Graph contains a cycle. Detected by `validate()`; `from_dict(partial=True)` also reports on serialized cycles. `plan()` raises `CycleInWorkflowError` instead. |
| `type_mismatch` | Upstream output's `ImageSpec` is incompatible with the downstream input's `ImageSpec`. |
| `missing_input` | A required input has no column binding, no constant, and no `Inputs` default. Also reported for serialized edges referencing an unknown upstream node. |
| `unknown_input` | A keyword argument does not correspond to any declared `Inputs` field. |
| `column_not_found` | A `ColumnRef` targets a column that does not exist in the upstream's `Outputs`. |
| `parameter_invalid` | A constant value fails Pydantic validation against its `Inputs` annotation. Produced only by `validate()` (and the module-level `validate_parameters()` helper). |
| `unknown_tool` | `from_dict` could not resolve the tool module / class / versioned package. |
| `duplicate_name` | Two nodes in the workflow share the same name. |
| `construction_failed` | Catch-all for unexpected failures during node construction (e.g., the tool class's `__init__` raised). |
| `source_tool_upstream` | A source `DataFrameTool` (`accepts_upstream = False`) was constructed with positional upstream arguments. |

Mapping to existing exceptions:

- `BindingError` → `missing_input` (default), `type_mismatch`, or `unknown_input`, depending on the failure context.
- `ColumnNotFoundError` → `column_not_found`.
- `IndexAlignmentError` → `construction_failed`.
- `SourceToolUpstreamError` → `source_tool_upstream`.

Helpers are provided on each domain exception — `.to_validation_error(node, field=..., kind=...)` — for callers who want to convert an exception they caught into a `ValidationError` with the appropriate kind.

The module-level helper `bioimageflow.validate_parameters(tool_class, parameters)` returns `list[ValidationError]` for a single node's constants without needing a Workflow. The module-level helper `bioimageflow.check_type_compat(node, field, col_ref)` returns `ValidationError | None` for a single column binding. `bioimageflow.serialize_image_spec(spec)` returns a JSON-friendly dict representation of an `ImageSpec` (`{"semantics": [...], "layouts": [...], "dtypes": [...], "formats": [...]}` with enum value strings) — exposed in `get_inputs_schema(tool)[field]["image_spec_serialized"]` for GUI use.

**Tool-level wire-format schema.** GUIs exposing a tool's schema over the wire should use `bioimageflow.validation.serialize_input_schema(tool_class)` and `serialize_output_schema(tool_class)` — the canonical, JSON-safe representation. Both accept the tool class (no instantiation), return `{}` for tools without `Inputs` / `Outputs`, and serialize `connectable` as one of `"never" | "not_by_default" | "by_default"`. `Outputs` that subclass `Passthrough` are serialized as the marker `{"_passthrough": True}`. `required` is determined by presence of a class-level default — it is orthogonal to whether the field's type is `Optional[X]`. See §2.4 for the full field shape.

---

## 7. File Management

### 7.1 Output Templating Engine

BioImageFlow enforces structured file naming to prevent overwrites and maintain order. Path output fields in `ProcessingTool.Outputs` with `Template(...)` defaults are treated as path templates, resolved by the engine before dispatch. (DataFrameTool does not use output templating — it returns DataFrames directly.)

**Template declaration rule:** Templates must be declared explicitly with `Template("...")` on fields whose type annotation is Path-based (`Path` or `Annotated[Path, ...]`). Non-path fields cannot declare `Template(...)`. Plain string or `Path` defaults containing template expressions are invalid and raise an error.

**Available template variables:**

| Variable                   | Description                                            |
|---------------------------|--------------------------------------------------------|
| `{node_name}`             | Name of the current node/step                          |
| `{row_index}`             | Global index of the item in the DataFrame              |
| `{<input_field>.name}`    | Original filename of the named input path field        |
| `{<input_field>.stem}`    | Filename without extension                             |
| `{<input_field>.ext}`     | Last file extension (e.g., `.gz`)                      |
| `{<input_field>.exts}`    | All file extensions (e.g., `.tar.gz`)                  |
| `{<input_field>}`         | Value of an input field, useful for scalar parameters  |
| `{column:<column_name>}`  | Value from the named DataFrame column for this row     |
| `{timestamp}`             | Execution timestamp                                    |

`<input_field>` must be the name of an `Inputs` field typed as a path (e.g., `input_image`).

**Default template:** Path outputs without a `Template(...)` default use `{node_name}_{row_index}{ext}` when the tool has exactly one path input, otherwise `{node_name}_{row_index}`.

**`{ext}` resolution:** If the tool has exactly one input path field, `{ext}` resolves to its extension. Otherwise (zero or multiple input paths), `{ext}` resolves to an empty string — the tool author must specify the extension explicitly in the template (e.g., `.tif` or `{input_image.ext}`).

**Example:**
```python
class Outputs(IOModel):
    mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("{input_image.stem}_mask_{row_index}.png")
```
For a row where `input_image` is `/data/cell_01.tif` and `row_index` is `3`, this resolves to `cell_01_mask_3.png`.

**1-to-N output naming:** The resolved template passed in `Arguments` acts as a **base path**. The tool may mutate it to create non-colliding names (e.g., `base_path.with_name(f"{base_path.stem}_tile{i}{base_path.suffix}")`). The engine builds the output DataFrame from the paths returned in `Outputs` objects, not from the pre-resolved templates.

### 7.2 Directory Structure

The runtime storage layout is rooted at `Workflow.storage_path`.
When the platform runs a saved workflow, it passes `<workflow-directory>/results/` as the required runtime storage path.
The workspace contains the saved workflow tree, workspace-local custom tools, user data, and co-located workflow results:

```text
workspace/
  tools/
  workflows/
    <folder>/<workflow>/
      workflow.json
      results/
        ... runtime storage layout below ...
  data/
```

```text
/workflow_storage_root/
  cache/
    v1/
      results/
        <result-shard>/
          <result-key>/
            attempts/
              <attempt-id>/
                attempt.json
                staging/
                  dataframe.parquet
                  dataframe.csv           # optional debug artifact
                  assets/
                  work/
            records/
              <record-id>/
                manifest.json
                dataframe.parquet
                dataframe.csv             # optional debug artifact
                assets/
            current.json
            conflicts/
      transient/
        runs/
          <run-id>/
            nodes/
              <node-key>/
                <invocation-id>/
                  invocation.json
                  assets/
                  work/
                  failed.json            # optional
  diagnostics/
    v1/
      runs/
        <run-id>/
          nodes/
            <node-key>/
              <invocation-id>/
                tasks/
                  <task-id>.json
  views/
    runs/
      <run-id>/
        run.json
        nodes/
          <node-key>/
            result.json
            record.bioimageflow-link.json
            outputs/
      latest-success.bioimageflow-link.json
    latest/
      <node-key>.bioimageflow-link.json
  outputs/
    runs/
    latest/
```

`cache/v1/` is the canonical machine-readable cache root.
Reusable records are immutable once published.
Non-reusable `ProcessingTool` invocations use the confined `cache/v1/transient/runs/` tree, create no records or views, and remain until explicit writer-safe transient cleanup.
Reusable attempts carry required running and terminal lifecycle metadata in `attempt.json`.
Backend task metadata is terminalized after future observation under `diagnostics/v1/` and never contributes to result keys or immutable record IDs.
`views/runs/` and `views/latest/` are portable JSON views over selected cache records and must not be used to decide cache hits.
Run and latest views use pointer files by default (`*.bioimageflow-link.json`) so the layout works on filesystems and platforms where symlinks are unavailable or inconvenient.
`outputs/runs/` and `outputs/latest/` contain optional materialized human-facing files created by `Workflow(storage_path=..., output_view=...)`, `Workflow.export_outputs(...)`, the top-level `bioimageflow.export_outputs(...)` function, or the `bioimageflow export-outputs` CLI.
Supported materialization modes are `pointer`, `symlink`, `copy`, and `hardlink`; `none` creates no `outputs/` projection and leaves only the canonical portable pointers under `views/`.
`pointer` creates portable `*.bioimageflow-link.json` files under `outputs/`; `symlink`, `copy`, and `hardlink` use the corresponding filesystem operations.
The canonical `cache/v1/`, `views/runs/`, and `views/latest/` layouts use record-relative paths.
The run-output hierarchy at `outputs/runs/<run-id>/nodes/<node-key>/outputs/` also uses record-relative paths.
The human-facing latest hierarchy uses node-relative paths: `assets/file.tiff` maps to `outputs/latest/<node-key>/file.tiff`, `assets/masks/nuclei/file.tiff` maps to `outputs/latest/<node-key>/masks/nuclei/file.tiff`, and a safe manifest path without a leading `assets/` maps in full beneath the node directory.
Scoped node paths form part of the node layer, and the complete mapped path set must be collision-free.
A latest node tree is fully built and validated in a temporary sibling before it is installed.
If materialization or installation fails, the currently published node tree stays usable or is restored; this is a failure-safety guarantee rather than a promise of filesystem-level atomic directory replacement.
Linked outputs refer to canonical cached files and should be treated as read-only, while copied outputs require additional storage.
Materialized files are disposable and are not canonical cache records.

Every materialized node output contains its declared assets plus the following data and explanation files:

```text
dataframe.parquet
dataframe.csv
dataframe.json
provenance.json
```

`dataframe.parquet` is the selected record's canonical table and follows the requested pointer, symlink, copy, or hardlink mode.
`dataframe.csv` and `dataframe.json` are derived readable exports written as regular files; the JSON representation uses an explicitly versioned split orientation.
For latest output views, owned-asset values in the readable table exports use the same simplified paths as the latest asset tree.
`provenance.json` is a self-contained explanation artifact assembled from the immutable record manifest and the run view.
It records the run and node identity, tool module/class/version, normalized parameters, environment and logical digests, cache-hit status, selected upstream result/record references and selectors, dataframe metadata, and declared outputs.
This intentionally overlaps `run.json`, node `result.json`, and the immutable record `manifest.json`: those files retain their canonical storage roles, while `provenance.json` makes a copied output understandable without navigating back into the cache.

The standalone copy API and CLI do not require reconstructing or loading the original workflow:

```python
from bioimageflow import export_outputs

export_outputs("./results", mode="copy", scope="latest")
```

An explicit external output root may be installed without reading from a disposable `outputs/` projection:

```python
export_outputs(
    "./results",
    destination="./shared-results",
    replace=False,
    mode="copy",
    scope="latest",
)
```

```bash
bioimageflow export-outputs ./results
bioimageflow export-outputs ./results --scope runs --run-id run_...
bioimageflow export-outputs ./results --destination ./shared-results --replace
```

The function and CLI default to `mode="copy"` and `scope="latest"`.
`Workflow.export_outputs(...)` retains its existing `mode="symlink"` default for API compatibility.
An explicit destination is a complete output root containing `latest/` and/or `runs/<run-id>/`.
It must be disjoint from source storage and is installed from a sibling staging tree.
Existing destinations require explicit replacement, and an installation failure restores the previous tree.
The `replace` flag is invalid without an explicit destination.

`run.json` records the effective injected backend and effective parallel or sequential scheduling policy.
The active `WorkflowExecutionContext` run ID is used consistently in attempt diagnostics, transient invocation diagnostics, guarded selection provenance, and run views, but never in result-key or record-ID material.

> outputs/latest contains the latest successful output independently for each node. Nodes may refer to different workflow executions after selected, failed, cancelled, or overlapping runs.

`Storage.probe_output_view_mode(mode)` tests a requested mode beneath the actual workflow storage root without changing cache selection or output state and returns an `OutputViewCapability` structured diagnostic.
The probe verifies readable file and directory symlinks, file hardlinks, copies, and portable pointers as appropriate.
Automatic disposable materialization failures are warnings and do not fail successful computation; explicit `Workflow.export_outputs(...)` calls remain strict.

`assets/` contains files that are part of a tool's declared `Outputs`.
Every manifest-owned asset declares `asset_type="file"` or `"directory"`.
Directory records use the canonical normalized, sorted recursive tree digest defined by the storage specification and reject symlinks, special files, traversal, normalized duplicates, and case-folding collisions.
`work/` is reserved for files that exist only to execute the tool: temporary images, implicit files created by external CLIs, unpacked models, and similar intermediates.
Files in `work/` are not part of the tool output contract, are not exposed as DataFrame outputs unless a tool explicitly returns and declares them, and are not included in result-key material or record identity unless they are promoted to declared assets during publication.
Resolved declared template outputs under `assets/` are promoted when they exist even if the tool returns zero dataframe rows, so per-object detectors can publish blank label images without fabricating object rows.
Declared `zero_row_scalar_outputs` values are promoted as manifest-only scalar metadata for input rows that produce no dataframe rows, so table-only detectors can record facts such as `spot_count=0` without fabricating object rows.

Package-local `data/` directories are read-only static resources shipped with the tool package. Tools must not generate or mutate files in package `data/` at runtime. If a static resource is missing in a development checkout and must be generated as a fallback, the tool generates it under a tool-named child of `ExecutionContext.work_dir`.

External command wrappers must avoid process-CWD pollution. If a row-level binary writes implicit files such as `LoG.tif`, the wrapper passes `cwd=context.row_dir` to `subprocess.run()` or equivalent. Batch-level wrappers use `cwd=context.batch_dir`. Shared generated runtime resources go under `context.work_dir`, preferably in a tool-named child directory. The engine does not change the process working directory globally.

The exhaustive storage contract is specified in [Output and Cache Storage Specification](reference/output_cache_storage.md).

---

## 8. Shared Memory Management

*Module: `bioimageflow_core.shm`*

BioImageFlow supports shared memory for high-throughput pipelines where disk I/O is a bottleneck. Shared memory is used exclusively by `ProcessingTool`.

### 8.1 Shared Memory Helpers

```python
@contextmanager
def create_shared_output(
    data: "np.ndarray",
    name: str | None = None
) -> "Iterator[SharedArray]":
    """
    Create a shared memory segment, copy data into it, and yield a SharedArray
    descriptor. The local handle is closed on exit — the tool cannot write to
    the segment after the with block. The data persists in shared memory until
    the engine unlinks it.

    If name is None, generates a unique name with the 'bif_' prefix.
    """
    ...

@contextmanager
def open_shared_array(ref: SharedArray) -> "Iterator[np.ndarray]":
    """
    Attach to an existing shared memory segment.
    Yields a zero-copy numpy array backed by shared memory.
    The local handle is closed on exit.
    """
    ...
```

Both helpers use `numpy` and `multiprocessing.shared_memory` at runtime. NumPy is declared by `bioimageflow-core`, so shared-memory APIs work in worker environments even when an individual tool did not list NumPy explicitly.

**Important: `close()` vs `unlink()`** — Both context managers **close** the local shared memory handle on exit but do **not unlink** (delete) the segment. The data persists after the `with` block ends so that downstream consumers and the engine can access it. This means `return` inside a `with create_shared_output(...)` block is correct and expected. Tool authors should never unlink shared memory themselves — only the engine does that.

**Usage in a ProcessingTool:**
```python
def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
    from bioimageflow_core.io import load_image
    from bioimageflow_core.shm import create_shared_output
    import imageio.v3 as iio

    with load_image(arguments.input_image, file_reader=iio.imread) as image:
        result = some_processing(image)

    with create_shared_output(result) as shm_ref:
        return self.Outputs(output_data=shm_ref)  # Safe: data outlives the handle
```

### 8.2 Lifecycle

- **Allocation:** Tools create shared memory segments using `create_shared_output()`, which uses the `bif_` namespace prefix.
- **Ownership:** When a tool returns a `SharedArray` in its outputs, the engine assumes full ownership. Since `create_shared_output` closes the tool's handle automatically, ownership transfer is enforced by the API.
- **Consumption:** Downstream tools read shared memory via `load_image()` (Path/SharedArray dispatch) or `open_shared_array()` directly.
- **Garbage Collection:** The helper closes local shared-memory handles but does not unlink segments automatically.
- **Crash Safety:** Shared-memory recovery is an application responsibility; no cleanup CLI is part of the public library contract.
- **Persistence:** Shared memory is volatile. If caching is requested for a source or column-bound `ProcessingTool` node that produced shared memory outputs, the engine publishes each non-null `SharedArray` output cell as a durable record-owned `.npy` asset under the reserved `assets/shm/` namespace. When the node is subsequently loaded from cache, the engine reads each durable asset back into a fresh `SharedArray` before dispatching to downstream tools, thereby strictly respecting the `ImageShared` interface contract.

When a tool stores a `SharedArray` in an output DataFrame column, it carries the shared memory name, array shape, and dtype — sufficient for downstream tools to attach and read the data. Since `SharedArray` is a frozen dataclass defined in `bioimageflow-core`, it is picklable and can cross the serialization boundary.

---

## 9. Error Handling

- **Binding errors** (`BindingError`): Raised at graph construction time when a required input field has no source (no column reference, no constant, no default). Lists the missing field and available sources.
- **Column not found** (`ColumnNotFoundError`): Raised at graph construction time when a column reference (`node["col"]` or node shorthand) refers to a column that does not exist in the upstream node's output schema. Includes available columns and close-match suggestions. For DataFrameTool upstreams without `Outputs`, this check is deferred to execution time.
- **Index alignment errors** (`IndexAlignmentError`): Raised at execution time when a ProcessingTool references columns from upstream nodes whose indices have no common lineage. The user must insert a merge DataFrameTool to combine the data explicitly.
- **Template errors**: Raised at graph construction time if a ProcessingTool output template references undefined variables or input fields.
- **Worker exceptions:** Remote exceptions are raised as `WorkerTaskError`; Parsl uses the public `ParslTaskError` subtype. Errors include scoped node, tool origin, route, task ID, invocation ID, optional cache attempt ID, row position/range, original type/message, and remote traceback when available.
- **DataFrameTool exceptions:** Exceptions raised in `merge_dataframes` or `transform` propagate directly since they run in the main process.
- **Disabled node errors** (`DisabledNodeError`): Raised at execution time when all requested target nodes are disabled or have disabled upstream dependencies. When only some targets are disabled in a multi-target `compute()` call, the disabled targets are silently omitted from the result dict.
- **Row-level failure:** When a single row fails in `process_row`, the entire node execution fails. The engine does not produce partial results.

Compilation assigns stable real-tool ordinals by deterministic topological order with scoped path as the ready-node tie breaker.
After stopping new submission and draining submitted work, the scheduler chooses the public primary failure by `(compiled node ordinal, first input position, task ID)`.
Completion timing never selects the primary error.

---

## 10. Resource Constraints

Processing tools declare minimum worker requirements through `ResourceSpec`.
Each `ProcessingTool` node may independently add public `NodeResourceOverrides`.
`DataFrameTool` and workflow-boundary nodes do not expose worker overrides.

```python
@dataclass(frozen=True)
class ResourceSpec:
    cpu: int = 1                    # Number of CPUs required
    gpu: int = 0                    # Number of GPUs required
    gpu_memory: str | None = None   # e.g., "8GB"
    max_concurrent: int = 0         # Max parallel rows (0 = unlimited)
    memory: str | None = None       # e.g., "16GB"

class MyGPUTool(ProcessingTool):
    resources = ResourceSpec(gpu=1, max_concurrent=4)
    ...

node = MyGPUTool()(name="segment")
node.set_resource_overrides(
    NodeResourceOverrides(gpu=2, memory="32GB", max_concurrent=2)
)
```

- A missing override field inherits the declaration.
- CPU, GPU, memory, and GPU-memory overrides are requirements and may not fall below the declaration.
- A non-zero `max_concurrent` override may lower but may not raise a non-zero declared cap. Zero means unlimited only when the declaration permits unlimited execution.
- `Node.effective_resources` and `effective_node_resources(node)` return the validated merged `ResourceSpec`.
- `ResourceSpec.max_concurrent` bounds both Wetlands 2 row submission and Parsl submission. Direct execution remains sequential within a node.
- Parsl validates each request against an explicitly attested executor slot and uses `max_concurrent` with `ParslTaskPolicy.max_in_flight` to bound unfinished submissions.
- Scoped-node routes, environment-identity routes, or one unique compatible binding select the executor; there is no implicit default, GPU, or environment-name route.
- Tools without an explicit declaration inherit `ResourceSpec()` defaults: one CPU, zero GPUs, no memory floors, and unlimited node concurrency.
- Overrides round-trip recursively in graph serialization and archives, belong to the node instance rather than the tool class, and do not affect result/cache keys.

`ResourceSpec` lives in `bioimageflow_core.environment` alongside `EnvironmentSpec`.

The Wetlands backend selects the environment pool size from `wf.get_environment(tool).max_workers` when non-zero and otherwise from `Workflow.max_workers`.
Wetlands 2 has no application-facing per-worker environment callback, so GPU device visibility is the responsibility of the environment or external runtime rather than implicit BioImageFlow mutation.

---

## 11. Logging

BioImageFlow uses Python's standard `logging` module with node-specific logger names. Engine and workflow construction do not attach console handlers; console logging is an explicit host/application concern.

```python
import logging
import bioimageflow

bioimageflow.configure_logging()

# Framework-level logger
logger = logging.getLogger("bioimageflow")

# Per-node loggers (created by the engine during execution)
node_logger = logging.getLogger(f"bioimageflow.node.{node_name}")
```

- `bioimageflow.configure_logging()` sets the BioImageFlow logger level to `INFO` by default and routes emitted `DEBUG`/`INFO` records to stdout and `WARNING`+ records to stderr using BioImageFlow-owned split stream handlers. Pass `level=logging.DEBUG` to emit debug records.
- `bioimageflow.configure_logging()` delegates Wetlands console setup to `wetlands.logger.enable_console_logging`; Wetlands 2 worker stdout and stderr are forwarded through its logging channel.
- BioImageFlow does not own Wetlands file logging. Wetlands file logs remain the responsibility of the Wetlands `EnvironmentManager`.
- A `JsonFormatter` is available for machine-readable output (structured event logging).
- Wetlands worker logs may be forwarded through its communication channel.
- Parsl task stdout/stderr is exposed only when the backend explicitly redirects it to unique diagnostics outside immutable records.
- Log levels follow standard Python conventions: `DEBUG` for per-row details, `INFO` for node lifecycle events, `WARNING` for compatibility warnings (e.g., unverified type constraints), `ERROR` for failures.

---

## 12. Parallelism

- **Direct backend (`engine="direct"`):** Runs `ProcessingTool` methods in the orchestrator process. This backend is for deterministic tests.
- **Wetlands backend (`engine="wetlands"`):** Dispatches `ProcessingTool` work to Wetlands worker environments. Within each node, `process_row` calls are dispatched via Wetlands task APIs. When the effective `max_workers > 1`, rows may run in parallel across worker processes. When `max_workers == 1` (default), rows run sequentially within that environment.
- **Parsl backend (`engine="parsl"`):** Dispatches `ProcessingTool` task envelopes to explicitly routed and preflighted Parsl executors. Submission is bounded and collection is deterministic.
- **Parallel scheduling (`execution="parallel"`):** Independent ready nodes may execute concurrently. `DataFrameTool` nodes still run on the main thread with coordination because they operate on DataFrames in the orchestrator process and may not be thread-safe.
- **Sequential scheduling (`execution="sequential"`):** Forces single-node-at-a-time execution for debugging and deterministic reproduction.

The choice of backend and scheduling policy is transparent to tool authors.
Direct, Wetlands, and Parsl execute the same `ProcessingTool` methods and share output, cache, progress, and failure semantics.

---

## 13. Cancellation

Workflow execution is scoped by a runtime-only `WorkflowExecutionContext`.

```python
class WorkflowExecutionContext:
    run_id: str | None
    defer_success_finalization: bool

    @property
    def cancel_requested(self) -> bool: ...

    def request_cancel(self) -> None: ...
    def finalize_success(self) -> None: ...
    def finalize_failure(self, error: BaseException) -> None: ...
```

`compute(..., run_context=context)` and `compute_steps(..., run_context=context)` use the supplied context.
When omitted, each public call creates a fresh context and run ID.
The exact context propagates through synthetic root wrappers and recursive execution.
One `Workflow` permits one active public execution; overlap fails before compilation, run-view mutation, cache lookup, or resource startup.
A supplied context preserves a cancellation request made before startup.
Finalization is idempotently bound to one execution.
`defer_success_finalization=True` leaves successful execution non-terminal for the submitted launcher, while failure and cancellation finalize immediately; invalid or mismatched finalization raises `RuntimeError`.

```python
import threading

wf = Workflow(storage_path="./results", on_progress=my_callback)
# ... build graph ...

thread = threading.Thread(target=lambda: wf.compute(target_node))
thread.start()

# Later, from the main thread or a GUI button:
wf.cancel()
thread.join()
```

**Cancellation semantics:**

1. `Workflow.cancel()` signals only the active context; calling it while idle has no effect on a later execution.
2. A supplied context may already be cancelled before startup, and that state is not cleared.
3. The scheduler stops submitting new nodes and row tasks.
4. Each backend requests cancellation of its unfinished submitted work.
5. Late results are ignored for construction and publication.
6. Every submitted task and possible writer is drained before return, engine reuse, or owned-resource cleanup.
7. An incomplete attempt or transient invocation is never selected or exposed through views.
8. Records selected by completed nodes remain valid.
9. A running `DataFrameTool` is not interruptible; cancellation is observed after it returns or raises.
10. The engine emits `cancelled` progress and raises `WorkflowCancelledError`.

Wetlands may additionally expose cooperative worker cancellation to tool code.
Parsl does not support worker-side cooperative cancellation.

```python
class MyTool(ProcessingTool):
    def process_row(self, arguments: Arguments, *, task=None) -> Outputs:
        for i in range(1000):
            if task and task.cancel_requested:
                task.cancel()  # acknowledge — return value is irrelevant
                return
            do_work(i)
        return self.Outputs(...)
```

Because `compute_steps()` has a lazy body, it reserves or attaches its execution context at the public call boundary.
Exhausting or explicitly closing the iterator detaches the context and applies cleanup after drain.

---

## 14. Import Cheat Sheet

```python
# === bioimageflow-core (available in all environments) ===
from bioimageflow_core import (
    # Types
    Semantic, Layout, ImageSpec, SharedArray, ImageShared,
    SCALAR_IMAGE_SEMANTICS, check_compatibility,
    # Environment
    EnvironmentSpec, GENERAL_ENV, ResourceSpec,
    # Tool
    BaseTool, ProcessingTool, IOModel, Category, GUIMeta,
    # Arguments
    Arguments,
    # Strict remote-processing protocol and origins
    ProcessingTaskV1, RowInvocationV1,
    ProcessingTaskResultV1, RowResultV1,
    InstalledModuleOriginV1, VersionedModuleOriginV1,
    SharedModuleOriginV1, SourceFileOriginV1, ArchiveModuleOriginV1,
)
from bioimageflow_core.io import load_image, save_image
from bioimageflow_core.shm import create_shared_output, open_shared_array
from bioimageflow_common_tools import InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect

# === bioimageflow (main process only) ===
from bioimageflow import (
    DataFrameTool, Passthrough,
    Workflow, OutputView,
    DefaultEngine, SequentialEngine, ParslEngine,
    ResourceLifetime, WorkflowExecutionContext, WetlandsEnvManager,
    ParslTaskPolicy, WorkerSlotCapacity, ExecutorCapabilities,
    WorkerEnvironmentAttestation, ExecutorBinding,
    ParslTaskError, WorkflowCancelledError,
    # Submitted and remote Parsl execution
    ParslConfigRef, OrchestratorLaunchConfig, PSIJLaunchConfig,
    SSHSubmissionTransport, LocalUpload,
    WorkflowRun, RemoteWorkflowRun, submit_workflow,
    BackendNotSupportedError, PSIJSubmissionUncertainError,
    WorkflowRunFailedError, WorkflowRunLostError,
    WorkflowRunNotReadyError, WorkflowRunResultUnavailableError,
    LauncherError, LauncherProtocolError, LauncherStateConflictError,
    WorkflowNode,
    # Versioned tool loading and PEP 723 support
    load_versioned_package, unload_versioned_package, get_tool_package_info,
    require_tool_packages,
    # GUI / platform integration
    WorkflowSession,
    ToolRegistry, ToolMetadata,
    # Pre-execution planning
    NodePlan, NodePlanStatus, NodeStep,
    DisabledNodeError, CycleInWorkflowError,
    # Validation surface
    ValidationError, ValidationErrorKind,
    validate_parameters, check_type_compat,
    serialize_image_spec, serialize_constant, deserialize_constant,
    serialize_input_schema, serialize_output_schema, SchemaSerializationError,
    get_inputs_schema,
)
from bioimageflow.node import Node, ColumnRef
from bioimageflow.tool_loader import resolve_tool_class
```

---

## 14. Unified Recursive Workflows

There is one workflow definition type at every nesting level: `Workflow`.
Calling a workflow inside an active, distinct parent captures an independent structural snapshot and returns a public `WorkflowNode`.
Factories in reusable Python modules export the exact symbol `build_workflow(*, storage_path)` and return a fresh `Workflow`.
They may provide a file-local default such as `Path(__file__).resolve().parent / "results"` for direct standalone use.

### 14.1 Interface construction

`Workflow.input(name, annotation=None, *, kind="field", default=MISSING, id=None)` declares a stable input port and returns a symbolic reference owned by that workflow.
Field inputs target named tool fields or child-workflow field ports.
DataFrame inputs target positional `DataFrameTool` inputs or child-workflow DataFrame ports.
A symbolic input can fan out to multiple compatible targets, but cannot be used outside its active owner.

`Workflow.output(name, source, *, id=None)` publishes an internal `ColumnRef`.
Interface names are unique across inputs and outputs, while IDs are immutable wire identities.
The name `name` is reserved for invocation node naming and cannot be an input name.

### 14.2 Invocation and execution

`workflow(name=None, **bindings)` validates names and kinds, snapshots the definition, registers one `WorkflowNode` in the parent, and never executes a factory.
`workflow_node.workflow` is the editable definition for that invocation; editing it does not affect its source or siblings.
`workflow_node["output_name"]` resolves the name to the stable output-port ID.

Root values use `workflow.compute(inputs={...})`.
Field inputs receive ordinary values at root and constants or `ColumnRef` values when nested.
DataFrame inputs receive complete DataFrames at root and upstream nodes when nested.
Explicit values override interface defaults, which override local tool defaults.

The compiler recursively expands workflow nodes into scoped executable paths such as `outer/inner/tool`.
It assigns stable real-tool ordinals by deterministic topological order with scoped path as the ready-node tie breaker.
Every enabled internal terminal is a completion dependency, including detached branches.
Published output dependencies contribute values and provider/selector recipes that resolve selected real-provider records at runtime; completion-only dependencies do not change unrelated downstream identity.
A zero-output workflow executes its terminals and returns a zero-row, zero-column DataFrame.
`compute_steps()` yields real tool steps only; `plan()` additionally reports aggregate workflow-node entries.

### 14.3 Strict recursive graph and archive formats

`Workflow.to_dict()`, `Workflow.from_dict()`, `Workflow.load()`, and `Workflow.export()` accept only `schema_version: 1`.
A recursive node has `"type": "workflow"`, an inline `workflow` graph, and constant `bindings` keyed by stable child-input IDs.
Tool nodes use `"type": "tool"`.
Edges have explicit `"column"` or `"dataframe"` variants and stable IDs.
Unknown variants, extra fields, malformed endpoints, duplicate IDs, and unversioned graphs are errors.

The portable archive envelope is separate from the graph:

```json
{
  "archive_version": 1,
  "workflow": {"schema_version": 1, "...": "..."},
  "custom_sources": []
}
```

The source table is collected once across the recursive graph.
Tool records refer to it through `source_module`, so equal class names from different source IDs cannot shadow one another.
Export serializes the already-materialized graph and never calls a factory again.

### 14.4 Trusted Python loading

`Workflow.from_python(path_or_module, storage_path=...)` loads the exact `build_workflow` symbol and calls it once as `build_workflow(storage_path=storage_path)`.
The return value must be a standalone `Workflow`.
File-based loading captures local Python sources before import, materializes them in a fresh import context, and captures custom-source records before that context is released.
Repeated calls therefore observe changes to the entry module or local helpers without stale `sys.modules` state.

The normative host-facing grammar, identifiers, status rules, and golden fixtures are specified in [Unified workflow contract](reference/unified_workflow_contract.md).

## Appendix A: Wetlands API

BioImageFlow requires Wetlands 2.
Wetlands 2 separates manager construction, observable provisioning, managed environments, worker pools, and execution tasks.
BioImageFlow uses its public top-level imports only.

### A.1 Environment Manager

```python
from wetlands import EnvironmentManager

manager = EnvironmentManager(
    root="wetlands/",
    pixi_executable="path/to/pixi",  # optional
)
```

Manager construction stores configuration and does not provision an environment.
`configure_wetlands(root=..., pixi_executable=..., network=..., termination_grace=...)` exposes the corresponding BioImageFlow configuration.
The old `wetlands_instance_path` and `conda_path` keywords are accepted as explicit migration aliases, while `main_conda_environment_path`, manager selection, and debug-mode construction are rejected.

### A.2 Provision an Environment

```python
from wetlands import EnvironmentSpec

spec = EnvironmentSpec(
    python="3.12.*",
    conda=("cellpose==3.1.0",),
    pypi=("bioimageflow-core>=0.1.0",),
)
environment = manager.provision("cellpose_env", spec).wait_for()
```

BioImageFlow translates its own immutable environment declaration into this Wetlands 2 value and includes `bioimageflow-core`.
Provisioning is lazy on the first uncached node that needs the environment.
The observable provisioning operation is always awaited through `wait_for()`.

### A.3 Start a Worker Pool

```python
pool = environment.start(workers=4, worker_timeout=300)
```

The returned `WorkerPool` owns its workers and is closed during the selected BioImageFlow resource lifetime.
Wetlands 1 `launch()`, `exit()`, and `worker_env` APIs are not used.

### A.4 Task-Based Execution

```python
task = pool.submit_import(
    "bioimageflow_core.worker:execute_processing_task",
    args=(request,),
)
result = task.wait_for()
```

BioImageFlow observes public `ExecutionEvent`, `ExecutionEventKind`, `ExecutionState`, and structured `ExecutionFailure` values.
Worker exceptions are converted to `WorkerTaskError` and then to the engine-neutral `NodeFailureDiagnostic`.

### A.5 Progress Reporting and Cancellation (Worker Side)

The BioImageFlow worker entry point remains responsible for its portable task context.
Wetlands task event listeners are adapted to public `ProgressEvent` callbacks.
Cancellation requests stop new work, cancel outstanding Wetlands tasks where supported, and drain terminal task states.

### A.6 Cleanup

```python
pool.close()
manager.close()
```

Cleanup is idempotent through `WetlandsEnvManager.stop()` and `close()`.
BioImageFlow never uses Wetlands private environment storage or transport internals.
