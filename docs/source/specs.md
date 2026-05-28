# BioImageFlow Library Specifications

## 1. Introduction and Scope

**BioImageFlow** is a Python library for orchestrating bioimage analysis workflows. Users chain discrete processing steps (**Tools**) into a Directed Acyclic Graph (**DAG**) where data flows between tools via DataFrames.

BioImageFlow addresses three challenges in bioimage analysis:

1. **Environment Isolation:** Tools often require conflicting dependencies (e.g., different Python versions, conflicting CUDA libraries). Each tool runs in its own isolated Conda environment.
2. **Data Provenance:** Every execution is hashed and cached, making it possible to trace exactly which parameters and logic produced a specific result.
3. **Type Safety:** A rich typing system prevents wiring errors such as feeding a CSV file to a tool that expects a segmentation mask.

### 1.1 Wetlands Integration

BioImageFlow relies on **Wetlands**, an external library for Conda environment isolation.

- **Wetlands** is a lightweight manager that creates Conda environments on demand from a dependency specification (e.g., `{"conda": ["cellpose==3.0"]}`).
- **BioImageFlow** is the orchestrator: it decides *what* to run and *in which order*. **Wetlands** is the executor: it spins up isolated environments and runs Python code inside them.
- Wetlands environments are created lazily (on first use) and kept alive for the duration of the workflow execution.
- Communication between the main process and worker environments uses Python's `multiprocessing.connection`, so all transferred objects must be picklable.
- Exceptions raised in the worker are automatically re-raised in the main process with their original stack trace.
- When `max_workers > 1` is passed to `env.launch()`, Wetlands starts multiple worker processes sharing the same Conda environment on disk. Tasks are dispatched to idle workers automatically; when all workers are busy, tasks queue internally. This enables `process_row` calls within a single node to run in parallel.
- BioImageFlow uses Wetlands' `env.map_tasks()` for row-level dispatch and `env.submit()` for batch dispatch, replacing the proxy-based `import_module()` pattern.

For Wetlands API details, see [Appendix A: Wetlands API](#appendix-a-wetlands-api).

### 1.2 Package Architecture

BioImageFlow is split into two packages:

**`bioimageflow-core`** — The shared foundation. Installed in the main process **and** in every tool worker environment. Contains the type system, tool base classes (`BaseTool` and `ProcessingTool`), argument passing, and I/O dispatch helpers. **Zero external dependencies** — uses only the Python standard library, ensuring it can never conflict with tool dependencies regardless of their numpy, pydantic, or imageio versions. Modules that touch numpy (`io.py`, `shm.py`) do so via runtime `import` — they borrow numpy from the tool's own environment rather than declaring it as a package dependency.

**`bioimageflow`** — The orchestrator. Installed only in the main process. Contains the graph engine, execution engines, column resolution, cache management, workflow coordination, `DataFrameTool` base class, and merge strategies. Depends on `bioimageflow-core`, `pandas`, `pydantic`, a graph library, and `parsl` (optional).

This split ensures that worker environments carry only the minimal footprint needed to run tool logic, while the main process has the full orchestration capabilities.

```text
bioimageflow-core (all environments)       bioimageflow (main process only)
├── types.py        # Type system          ├── dataframe_tool.py # DataFrameTool base class
├── environment.py  # EnvironmentSpec      ├── merge.py        # Built-in merge DataFrameTools
├── tool.py         # BaseTool,            ├── resolution.py   # Column resolver
│                   #   ProcessingTool,    ├── template.py     # Output templating
│                   #   IOModel, GUIMeta   ├── cache.py        # Hash & cache
├── arguments.py    # Arguments            ├── storage.py      # File management
├── io.py           # I/O dispatch (*)     ├── node.py         # Node, ColumnRef
└── shm.py          # Shared memory (*)    ├── engine.py       # Execution engines
                                           ├── validation.py   # Pydantic validation
                                           ├── tool_loader.py  # Versioned package loading
                                           └── workflow.py     # Workflow container

(*) io.py and shm.py use numpy at runtime via import — not as a declared
    dependency. They work because tools that process images always have
    numpy installed. Tools that never touch images or shared memory
    need not have numpy at all.
```

The framework automatically adds `bioimageflow-core` to the dependencies of every Wetlands environment.

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

This check is used during [input binding](#45-input-binding-logic-graph-construction) to validate that a column reference's upstream type is compatible with the consuming input field's type.

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
    "choices": ["fast", "accurate"], # from Literal[...] / Enum, or None
    "image_spec": {...},             # serialize_image_spec(...) or None
}
```

The `type` display name follows deterministic rules: bare Python types use `__name__` (`"int"`, `"float"`, `"str"`, `"bool"`, `"Path"`); `list` / `dict` / `tuple` generics collapse to `"list"` / `"dict"` / `"tuple"`; `Literal[...]` uses the type of the first literal (the enumeration is carried by `choices`, not `type`); `Enum` subclasses become `"str"`; `Annotated[X, ...]` unwraps to `X`; `Optional[X]` / `X | None` uses the display name of `X` (None-ness is expressed by `required`, not by `type`); `Annotated[Path, ImageSpec(...)]` and `ImageShared(...)` emit `"ImageFile"` and `"ImageShared"` respectively. The reserved value `"any"` denotes a column whose runtime type is unknown — emitted by `resolve_outputs` / `resolve_merge_schema` for dynamic columns whose name (but not concrete type) is known at graph-construction time, and by `Concat.resolve_merge_schema` when two upstream schemas declare the same column with conflicting types.

The `connectable` field uses three-state strings: `"never"` (no pin, no toggle), `"not_by_default"` (pin hidden by default, a GUI checkbox reveals it), and `"by_default"` (pin visible by default, a GUI checkbox can hide it). Callers that only care whether a field has a pin should treat both `"not_by_default"` and `"by_default"` as connectable.

`required`, `nullable`, and the type-display rules are three orthogonal concerns:

- `required` is determined solely by presence of a class-level default on `Inputs`: a field with no default is `required=True`, even when its type is `Optional[X]` or `X | None`. A caller of a tool whose field is typed `Optional[int]` with no default must pass *something* — but `None` is acceptable when `nullable=True`.
- `nullable` is determined solely by the type annotation: `True` iff the annotation (after unwrapping `Annotated[...]`) is a `Union` whose args include `NoneType`. It is independent of whether a default exists. GUIs should use `nullable` (not `required`) to decide whether to expose a "set to null" affordance.
- The `type` display name strips `None` from unions — `Optional[int]` displays as `"int"` — because the None-ness is carried by `nullable`, not by `type`.

Output fields are simpler: `{"type": str, "default": Any | None, "image_spec": dict | None, "template": str | None}`. `template` is present when a `ProcessingTool` path output declares a `Template(...)` default. If an output annotation carries `GUIMeta`, the serialized output entry also includes JSON-safe `GUIMeta` fields (`connectable`, `display_name`, `description`, `group`, `min`, `max`, `step`) so GUIs can label output pins and tooltips. When `Outputs` is a `Passthrough` subclass (see §3.5 `DataFrameTool`), `serialize_output_schema` returns the marker `{"_passthrough": True}` — GUIs should render this as "inherits upstream columns".

Callers that want the Python-facing objects (raw `type`, raw `Connectable`) should keep using `get_inputs_schema(tool)` instead; the two APIs are complementary.

### 2.5 Interface Type Constraints

`Inputs` and `Outputs` models must use only standard-library types and `bioimageflow-core` metadata types such as `ImageSpec`, `GUIMeta`, and `ImageShared`. File-based image fields use `Annotated[Path, ImageSpec(...)]`. Third-party types (NumPy arrays, PIL images, etc.) are **not** allowed in the interface — they cannot cross the serialization boundary. `Outputs` is required on `ProcessingTool` (defines the serialization contract and output templates). On `DataFrameTool`, `Outputs` is optional — when declared, it enables construction-time validation of downstream column references (see [Section 3.4](#34-dataframetool)).

**Runtime type resolution:** File-based image annotations and `ImageShared` are distinct for graph-level compatibility checking (`check_compatibility`), but the orchestrator's Pydantic model builder resolves both to `Union[Path, str, SharedArray]` at validation time. This is necessary because caching may convert a `SharedArray` output to a file `Path` (see [Section 8.2](#82-lifecycle)), and the reverse can happen when shared memory is enabled. Tools should use `load_image()` which handles both transparently.

---

## 3. Tool Definition

BioImageFlow provides two kinds of tools, each with a single execution context:

- **`ProcessingTool`** — runs computation in an isolated Wetlands environment. Every method the tool author implements (`process_row`, `process_batch`) executes in the worker.
- **`DataFrameTool`** — transforms DataFrames in the main process. The single `transform` method has full access to Pandas.

Both inherit from `BaseTool`, which provides shared identity attributes (`name`, `category`, `tags`, `Inputs`) and graph wiring via `__call__`.

### 3.1 EnvironmentSpec

*Module: `bioimageflow_core.environment`*

Processing tools declare their environment requirements via an `EnvironmentSpec` object. This object is defined **once** and shared by reference across all tools that use the same environment.

```python
@dataclass(frozen=True)
class EnvironmentSpec:
    """Defines a reusable Wetlands environment specification."""
    name: str          # Wetlands environment name (e.g., "cellpose")
    dependencies: dict  # Wetlands format: {"conda": [...], "pip": [...], "python": "3.12"}
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
    dependencies={"conda": ["stardist==0.9", "tensorflow"], "python": "3.11"}
)
```

Multiple tools reference the same `EnvironmentSpec`. The framework passes `spec.name` and `spec.dependencies` to `wetlands.EnvironmentManager.create()`. If an environment with that name already exists, BioImageFlow validates a dependency hash match before reuse; on mismatch it raises `EnvironmentMismatchError` describing expected vs existing dependencies.

**Dependency normalization:** Before hashing, the framework normalizes the dependency specification to avoid false mismatches:
- Dependency lists are sorted alphabetically (e.g., `["numpy", "cellpose"]` and `["cellpose", "numpy"]` produce the same hash).
- Version strings are normalized to PEP 440 canonical form (e.g., `"3.0"` and `"3.0.0"` are treated as equivalent).
- Whitespace is stripped from dependency strings.

It is possible to directly define the EnvironmentSpec in ProcessingTool.environment if only one tool requires the environment.

#### Pre-Built General Environment

*Module: `bioimageflow_core.environment`*

`bioimageflow-core` provides a pre-defined `GENERAL_ENV` constant — a standard scientific image processing environment that covers the majority of "glue" tools. Tools that only need common packages (numpy, scipy, scikit-image, imageio, tifffile, Pillow) should use `GENERAL_ENV` instead of declaring ad-hoc environments.

```python
from bioimageflow_core import GENERAL_ENV

GENERAL_ENV = EnvironmentSpec(
    name="bioimageflow-general",
    dependencies={
        "python": "3.12",
        "pip": [
            "numpy",
            "scipy",
            "scikit-image",
            "imageio",
            "tifffile",
            "Pillow",
        ]
    }
)
```

**When to use `GENERAL_ENV`:** Tools whose only runtime dependencies are a subset of the packages above. For example, a tool that reads an image with imageio, processes it with numpy, and writes it back — no need to declare a custom environment.

**When NOT to use `GENERAL_ENV`:** Tools that require specialized libraries (cellpose, stardist, SimpleITK, bioio, opencv, etc.) still declare their own `EnvironmentSpec`. The general env catches the long tail of tools that just need standard scientific Python.

**Engine behavior:** `GENERAL_ENV` is a regular `EnvironmentSpec` — no sentinel, no magic. The engine creates it on first use and reuses it for all tools referencing it. All tools with `environment = GENERAL_ENV` share a single Wetlands worker process.

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import ProcessingTool, GENERAL_ENV, IOModel, Arguments, ImageSpec, Semantic, Template

class ExtractChannel(ProcessingTool):
    name = "extract_channel"
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

The `task` parameter also provides cooperative cancellation via `task.cancel_requested` (see [Cancellation](#cancellation)).

**Execution scratch context:** `process_row` and `process_batch` may declare an optional keyword-only `context: ExecutionContext` parameter. The engine injects it only when the method explicitly declares `context`; existing tools with `process_row(arguments)` or `process_batch(arguments_list)` are unchanged.

`ExecutionContext` is defined in `bioimageflow-core` and is picklable across the Wetlands serialization boundary:

```python
@dataclass(frozen=True)
class ExecutionContext:
    run_dir: Path       # storage_path/data/<node>/<timestamp>_<hash12>/
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
    name = "my_segmenter"
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
    name = "cellpose_segmenter"
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
    name = "cellpose_train"
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
    name = "other_tool"
    environment = cellpose_env  # Reuses the cellpose environment without inheriting
    ...
```

**ProcessingTool class attributes:**

| Attribute       | Type               | Description                                        |
|----------------|--------------------|----------------------------------------------------|
| `name`          | `str`              | Unique identifier for the tool                     |
| `documentation` | `str`              | Human-readable description                         |
| `category`      | `Category \| None` | High-level functional category (optional)          |
| `tags`          | `list[str]`        | Searchable tags                                    |
| `environment`   | `EnvironmentSpec`  | Wetlands environment specification (shared object) |
| `resources`     | `ResourceSpec`     | Optional resource requirements (GPU, memory, concurrency). See [Section 10](#10-resource-constraints). |

**Worker state warning:** State set on `self` during `__init__` (graph construction, main process) is **not** available in `process_row`/`process_batch` (worker process). For expensive resources like GPU models, use lazy initialization inside the processing method:

```python
class MyTool(ProcessingTool):
    _model = None

    def process_row(self, arguments):
        if self._model is None:
            self._model = load_model("weights.pth")  # Lazy init in worker
        result = self._model.predict(...)
```

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
    name = "filter_rows"

    class Outputs(Passthrough): pass  # Output schema = input schema (all columns preserved)

class CountLabelOverlaps(DataFrameTool):
    name = "count_label_overlaps"

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
| `name`          | `str`                                   | Unique identifier for the tool                 |
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

Built-in merge tools (`InnerJoin`, `CrossJoin`, `JoinOnColumn`, `Concat`, `Collect`) instead override `resolve_merge_schema(cls, upstream_schemas, inputs)` because their output columns depend on the *upstream* schemas, not just on their own inputs. The `Node.get_output_schema()` algorithm (next paragraph) prefers `resolve_merge_schema` over `resolve_outputs` when a merge tool overrides it.

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
    name = "column_regex"
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
    name = "filter_rows"
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
    name = "count_label_overlaps"
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

BioImageFlow provides built-in DataFrameTools for common merge operations in `bioimageflow.merge`. These override `merge_dataframes` and use the default `transform` (identity):

```python
class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index (default merge behavior)."""
    name = "inner_join"

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
    name = "cross_join"
    class Inputs(IOModel):
        suffixes: tuple = ("_left", "_right")


class JoinOnColumn(DataFrameTool):
    """Join upstream DataFrames on a named column (not index)."""
    name = "join_on_column"
    class Inputs(IOModel):
        join_column: str
        how: str = "inner"
        suffixes: tuple = ("_left", "_right")


class Concat(DataFrameTool):
    """Concatenate DataFrames vertically."""
    name = "concat"
    class Inputs(IOModel):
        pass


class Collect(DataFrameTool):
    """Gather columns from multiple ancestor nodes into one DataFrame.
    Convenience alias for InnerJoin — makes intent explicit when combining
    scattered columns from different pipeline branches."""
    name = "collect"
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
    Zero external dependencies — uses only standard-library features.
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

**Defaults:** Fields without a `GUIMeta` annotation default to `connectable: Connectable.NOT_BY_DEFAULT` with no numeric constraints, no group, and no display text or description. A GUI frontend inspects the `Annotated` metadata for each field; if no `GUIMeta` is found, it assumes the field is connectable but with the pin hidden by default, uses the field name as a fallback label, and provides no tooltip. Data input fields (image paths) should use explicit `GUIMeta(connectable=Connectable.BY_DEFAULT)` to make their pins visible.

**Usage:**

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import ProcessingTool, IOModel, ImageSpec, Semantic, Arguments, GUIMeta, Connectable, Template

class CellposeSegmenter(ProcessingTool):
    name = "cellpose_segmenter"
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

**Runtime behavior:** `GUIMeta` is purely declarative metadata — it has no effect on validation, execution, caching, or hashing. The orchestrator and worker environments ignore it entirely. It exists solely for GUI frontends to render appropriate widgets, labels, tooltips, and port visibility.

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
from bioimageflow.merge import CrossJoin, JoinOnColumn

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

Imports from `bioimageflow-core` (e.g., `ImageSpec`, `Arguments`, `IOModel`) can be at module level since the package is always available and has zero external dependencies.

DataFrameTool definitions import from `bioimageflow` and run exclusively in the main process, so they have full access to Pandas and any main-process library at module level:
```python
from bioimageflow import DataFrameTool
from bioimageflow_core import IOModel, Arguments
import pandas as pd  # Available — DataFrameTool runs in main process only
```

### 3.9 Image I/O

*Module: `bioimageflow_core.io`*

Since `bioimageflow-core` has no external dependencies, image I/O uses a **pluggable dispatch** pattern. The tool provides its own file reader/writer; `bioimageflow-core` handles the dispatch between file paths and shared memory references.

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

Tools are distributed as standard Python packages. The package version is used in the signature hash for caching (see [Section 6.1](#61-signature-hash)). When a tool's package version changes, cached results for that tool are automatically invalidated.

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

This applies everywhere: `__init__.py`, tool modules, SubWorkflow `build()` methods, and utility modules.

#### Tool Store

Tool packages are installed in a **tool store** — a directory under `~/.bioimageflow/tool_packages/` that holds versioned copies of each package. Multiple versions of the same package coexist as distinct directory trees:

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

Packages are installed via `pip install --target <dir> simpleitk-tools==X.Y.Z`, executed through Wetlands' pixi installation (no separate `pip` or `uv` on `PATH` required). The tool store path can be overridden via the `BIOIMAGEFLOW_TOOL_STORE` environment variable.

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
with Workflow() as wf:
    old_result = v1.GaussianSmooth()(input_image=raw["path"], sigma=1.0)
    new_result = v2.GaussianSmooth()(input_image=raw["path"], sigma=1.0)
    results = wf.compute(old_result, new_result)
```

The loading mechanism:

1. Creates a top-level module entry in `sys.modules` under the scoped name with `submodule_search_locations` pointing at the versioned directory.
2. Installs a temporary meta-path import hook so that relative imports within the package (e.g., `from .gaussian import GaussianSmooth`) resolve to the versioned directory under the scoped namespace.
3. Executes the package's `__init__.py`, which triggers all `from .xxx import ...` chains.
4. Stamps every `BaseTool` and `SubWorkflow` subclass found in the loaded modules with metadata: `_bif_package`, `_bif_package_version`, `_bif_canonical_module`.

This works transparently for all tool types:

- **ProcessingTools**: Loaded as real subclasses with real `process_row`/`process_batch`. `inspect.getfile()` returns the versioned path. Wetlands dispatch works unchanged.
- **DataFrameTools**: Loaded as real subclasses with real `transform()`/`merge_dataframes()`. They execute in the main process as usual.
- **SubWorkflows**: `build()` instantiates tools via relative imports (`from .gaussian import GaussianSmooth`). Since the entire package is loaded under the scoped namespace, relative imports resolve within that version's directory. Internal tools are automatically from the correct version.

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
configure_wetlands(wetlands_instance_path="./wetlands")

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
4. **Auto-installs missing packages** into the tool store via `pip install --target`, executed through Wetlands' pixi (so no separate `pip` or `uv` needs to be on `PATH`). Set `auto_install=False` to raise `FileNotFoundError` instead.
5. **Loads each package** via `load_versioned_package()`.
6. **Registers canonical names** in `sys.modules`: copies every scoped entry (e.g., `simpleitk_tools__1_0_0.gaussian`) to its canonical equivalent (`simpleitk_tools.gaussian`). This enables standard `from simpleitk_tools import GaussianSmooth` syntax.

This is safe because PEP 723 declares exactly one version per package — there is no ambiguity about which version to bind to the canonical name. For the advanced case of loading two versions of the same package simultaneously, use `load_versioned_package()` directly.

#### Auto-Install on JSON Load

`Workflow.load()` also auto-installs missing versioned packages. When a serialized workflow references `tool_package` and `tool_package_version`, the loader checks the tool store and installs via Wetlands' pixi if the package is absent. This means both `.py` scripts and `.json` workflow files are self-resolving — the user only needs `bioimageflow` (which bundles Wetlands) installed.

### 3.11 Tool Registry

`ToolRegistry` is the public, stateful index for tool classes loaded from versioned packages and for custom tools embedded in a workflow export. It is the surface GUIs and other host applications should use to enumerate and resolve tools — it wraps `load_versioned_package`, `resolve_tool_class`, workflow custom-tool discovery, and the schema serializers behind a single object so consumers do not have to rebuild metadata serialization or package-resolution layers themselves.

The registry deliberately separates **install** (slow, network-bound) from **register** (fast, in-process index lookup):

```python
from bioimageflow import ToolRegistry, ToolMetadata

reg = ToolRegistry()                                 # uses default tool store path
reg.install_package("cellpose_tools", "2.3.1")       # network: installs via Wetlands' pixi
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
the `custom_tool_modules` bundle written by `Workflow.export()`.

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

The registry indexes `BaseTool` and `SubWorkflow` subclasses; abstract base classes themselves are excluded. Multiple versions of the same package can be registered — they coexist as distinct entries because `resolve_tool_class` keys on the scoped module, not the class name alone.

### 3.12 Project-Local Custom Tools

A project may define custom `ProcessingTool`, `DataFrameTool`, or class-based
`SubWorkflow` classes in a project-local `tools/` package. In the BioImageFlow
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
  tools/
    __init__.py
    download_images.py
    measure_spots.py
    utils.py
    data/
      small_runtime_asset.json
  tests/
    test_tools.py
      tiny_input.csv
  outputs/
```

Rules:

- Tool classes live under `tools/` and are re-exported from `tools/__init__.py` for readable workflow imports.
- Helper modules, package constants, and small runtime assets needed by custom tools may also live under `tools/`. Use relative imports inside `tools/` so both the main process and workers can import the bundled package.
- Custom tools must have tests. Minimum coverage is schema serialization / validation plus one small execution test per custom tool behavior. Add integration coverage for tools that touch the workflow graph, output templates, environments, or sub-workflow boundaries.
- Committed test fixtures live under `tests/data/` and must be small. Generated
  outputs, caches, and downloaded files live under pytest temporary directories
  or the workflow `storage_path`. Platform-created workflows set that
  `storage_path` to a workspace-scoped output root such as
  `workspace/outputs/<workflow_id>/`.
- Static assets may be committed under `tools/data/` when they are small and intrinsic to the custom tool. Large binaries, trained models, downloaded datasets, and generated artifacts must be declared as dependencies, downloaded at runtime, or generated by the workflow.

Export behavior:

- `Workflow.export(path)` calls `to_dict(include_custom_tools=True)` and writes
  a top-level `custom_tool_modules` list containing a bundle for each used
  project-local `tools/` directory.
- The bundle preserves relative paths under `tools/` and includes file hashes plus an overall bundle hash. Generated/cache files such as `__pycache__`, `.pyc`, `.pytest_cache`, and hidden temp files are excluded. Export fails for unexpectedly large files.
- Regular tool nodes reference an embedded `tools/` bundle with `tool_source_module`; class-based sub-workflow nodes use `sub_workflow_source_module`.
- `Workflow.load(path)` validates the embedded bundle hash, materializes the `tools/` tree into a scoped temporary Python package, and resolves the class from that package before attempting package or normal import resolution.
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
    name = "dicom_loader"
    environment = EnvironmentSpec(name="dicom", dependencies={"conda": ["pydicom"]})

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
from bioimageflow.merge import CrossJoin, JoinOnColumn

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

- **Nodes** wrap a tool instance and its configuration (explicit arguments). Each node has a unique **node name** — either user-provided via `name=` in `__call__` or auto-generated from the tool's `name` attribute and a counter (e.g., `cellpose_segmenter_1`, `cellpose_segmenter_2`). Node names must be unique within a Workflow; the tool's `name` (class-level) may repeat across multiple nodes.
- **Edges** represent data dependency: an edge from Node A to Node B means Node B references columns from Node A (via `ColumnRef`) or receives Node A's output DataFrame (via positional argument to a DataFrameTool).
- **`ColumnRef`** is created by subscripting a Node: `node["col"]`. It records the upstream node and column name. The engine validates column existence at construction time using `Node.get_output_schema()` (see §3.5), which covers both static `Outputs` and dynamic-but-resolvable schemas — `Generate(column_name="x")["x"]` validates immediately, and a fully-configured merge tool (e.g. `CrossJoin(Files(...), Generate(column_name="sensitivity", ...))`) validates the union of upstream column names. Validation is deferred to execution time only when the schema cannot be resolved (e.g. an upstream merge whose own upstream is unresolvable).
- The graph must remain a DAG. Cycles are detected synchronously inside `__call__()` when the edge is created, providing instant feedback in scripts and notebooks.
- **Source nodes** are simply nodes with no upstream data dependencies — they are not a separate tool type or code path. Both tool types can act as source nodes:
  - A **DataFrameTool** with no positional arguments receives an empty `dfs` list in `merge_dataframes` and produces the initial DataFrame (e.g., by listing files in a directory).
  - A **ProcessingTool** with no `ColumnRef` or `Node` arguments (only constants or defaults) is executed through the same code path as any other ProcessingTool. With no column bindings, the engine uses a single-row index (`["0"]`), builds arguments from constants and defaults only, and dispatches to `process_row`/`process_batch` as usual. This is useful when listing or loading files requires specialized libraries (e.g., reading HDF5 headers, DICOM metadata, OME-TIFF pyramids) that should not pollute the main process.

**Wire-format edge entries.** In `Workflow.to_dict() / from_dict()` the `edges` list contains one dict per edge with the keys `from`, `to`, `column`, `field`, plus an optional opaque `id`:

```json
{"id": "e_42", "from": "loader_1", "to": "segmenter_1",
 "column": "path", "field": "input_image"}
```

`id` is opaque to the library: GUIs assign whatever stable identifier they want (e.g., to drive selection / hover state in the editor canvas). The library round-trips it through `to_dict` / `from_dict` and copies it onto every `ValidationError` raised against that edge (see [§6.6](#66-validation-error-reference)). For positional arguments (`column = field = "__positional__"`), `id` is the only way to disambiguate multiple edges between the same pair of nodes — `validate()`'s deduplication includes `edge_id` in the key.

### 4.3 The `Workflow` Object

The `Workflow` class holds the DAG graph object and provides configuration for storage, caching, execution engine, and progress monitoring.

**Creating a Workflow:**

```python
from bioimageflow import Workflow

# Option 1: Context manager (recommended). Nodes created inside are
# automatically registered with the workflow.
with Workflow(storage_path="./results", engine="sequential") as wf:
    raw = load_images(path="./data")
    masks = segment(input_image=raw["path"])
    results = analyze(image=raw["path"], mask=masks["mask"])
    final_df = wf.compute(results)

# Option 2: Explicit workflow. Pass the workflow to compute().
wf = Workflow(storage_path="./results")
raw = load_images(path="./data")
masks = segment(input_image=raw["path"])
final_df = wf.compute(masks)

# Option 3: Node.compute() creates an implicit default Workflow
# with default settings. Convenient for quick experiments.
raw = load_images(path="./data")
masks = segment(input_image=raw["path"])
final_df = masks.compute()  # Uses a default Workflow
```

Node registration is automatic: calling a tool (e.g., `segment(...)`) appends the resulting Node to the active Workflow (set by the context manager) or to a module-level default. `Node.compute()` is a shorthand that either uses the node's associated Workflow or creates a default one.

**Workflow constructor parameters:**

| Parameter       | Type          | Default         | Description                                      |
|----------------|---------------|-----------------|--------------------------------------------------|
| `storage_path`  | `str \| Path` | `"./bif_data"`  | Root directory for output files and cache. Relative values are interpreted against the orchestrator process working directory and stored internally as absolute runtime paths. |
| `engine`        | `str`         | `"sequential"`  | `"sequential"` or `"parsl"`                      |
| `max_executions`| `int`         | `0`             | Cache retention: number of past executions to keep |
| `max_age`       | `str \| None` | `None`          | Cache retention: max age (e.g., `"7d"`, `"24h"`) |
| `on_progress`   | `Callable \| None` | `None`     | Progress callback (see [Section 4.4](#44-progress-monitoring)) |

**`compute()` return type and terminal detection:**

```python
# No arguments: auto-detect all terminal nodes (nodes with no downstream dependents)
out = wf.compute()                  # -> dict[str, DataFrame] if multiple terminals, DataFrame if single

# Single terminal: returns DataFrame directly
df = wf.compute(results)            # -> DataFrame

# Multiple terminals: returns dict keyed by node name
out = wf.compute(results, masks)    # -> {"measure_stats_1": DataFrame, "cellpose_segmenter_1": DataFrame}

# Node.compute() always targets one node
df = results.compute()              # -> DataFrame
```

Shared upstream nodes are not re-executed — their cached results are reused.

**Workflow serialization:** Workflows can be exported and imported for reproducibility and sharing. The serialized form captures the full DAG structure, tool references, and parameter bindings:

```python
# Export
workflow.export("my_workflow.json")

# Import and re-execute
loaded = Workflow.load("my_workflow.json")
loaded.compute(loaded.nodes["measure_stats_1"])
```

The serialized format includes:
- Tool references (module path + class name for each node).
- Tool package info (package name + package version, when loaded from the tool store). This allows `Workflow.load()` to call `load_versioned_package()` and resolve the correct tool class.
- Project-local custom tool bundles, when a node uses a custom tool from the
  project's `tools/` package.
- Parameter bindings (constants, column references with upstream node names).
- Node enabled/disabled state (see [Section 4.6](#46-enabling-and-disabling-nodes)).
- Graph edges (upstream-downstream relationships).
- Workflow-level configuration (storage path, cache policy, engine choice).

Versioned package tool code is **not** serialized — the same tool packages (at
the referenced versions) must be available in the tool store to re-execute a
loaded workflow. Project-local custom tools are serialized into a top-level
`custom_tool_modules` bundle by `Workflow.export()`, and nodes reference that
bundle with `tool_source_module` or `sub_workflow_source_module`.
`Workflow.load()` uses the embedded `tools/` tree before falling back to package
or import resolution, so exported workflows carry their custom tools, helpers,
and small runtime assets across machines.

**In-memory serialization helpers** — for callers that work with dicts rather than files (GUI servers, test harnesses):

```python
# Produce the editable wire format without touching the filesystem
data: dict = workflow.to_dict()

# Include the project-local custom-tool bundle in memory.
export_data: dict = workflow.to_dict(include_custom_tools=True)

# Reconstruct from a dict (strict mode: raises on first error)
wf = Workflow.from_dict(data)

# Reconstruct as a non-raising diagnostic: returns (wf, errors).
# `partial=True` keeps building past failures; `validate_only=True`
# changes the return type to a tuple. Together they yield the
# original GUI "build everything you can" mode.
wf, errors = Workflow.from_dict(
    data,
    validate_only=True,         # return (wf, list[ValidationError]) instead of raising
    partial=True,               # keep building after per-node failures
    auto_install=True,          # default; False produces unknown_tool errors on missing packages
    storage_path_override=None, # override data["config"]["storage_path"] without mutating dict
)
```

`from_dict` accepts the same options as the `Workflow` constructor via keyword arguments (`on_progress`, `use_wetlands`, `wetlands_config`). When any of those is `None`, values from `data["config"]` or constructor defaults are used. `Workflow.load(path)` is preserved as a thin wrapper over `from_dict`.

The two flags compose orthogonally:

| `validate_only` | `partial` | Behavior |
|-----------------|-----------|----------|
| `False` (default) | `False` (default) | Strict: returns `Workflow`; raises on first failure. |
| `False` | `True` | Builds best-effort; raises an aggregated `ValueError` if any failure occurred. |
| `True` | `False` | Fail-fast diagnostic: returns `(wf, errors)` where `errors` has at most one entry. |
| `True` | `True` | GUI mode: returns `(wf, errors)` with all failures captured; `wf.is_partial` may be `True`. |

In partial mode the library maps construction failures to `ValidationError` entries (see the Validation Error Reference section) and produces a best-effort workflow with as much of the graph as could be wired. Subsequent calls to `workflow.validate()` and `workflow.plan()` remain meaningful on a partially-wired workflow.

**Build-time inspection.** After `from_dict`, callers can inspect what survived without a separate `validate()` call:

```python
wf, errors = Workflow.from_dict(data, validate_only=True, partial=True)
wf.errors          # list[ValidationError] — same list returned alongside wf
wf.failed_nodes    # dict[str, ValidationError] — nodes whose tool resolution / __init__ failed
wf.is_partial      # bool — True if any node from data["nodes"] is missing from wf.nodes
```

These properties are populated only by `from_dict`; for programmatic graph construction they are empty / `False`.

**Introspection helpers:**

```python
names: list[str] = workflow.topological_order()        # raises on cycle
deps: set[str] = workflow.downstream_of("loader_1")    # transitive downstream names, excluding argument
```

`topological_order` is a thin wrapper over `bioimageflow.engine.topological_order(workflow)`. If the graph may contain a cycle, call `workflow.validate()` first — the latter reports cycles via a `ValidationError` (`kind="cycle"`) instead of raising. `workflow.plan()` raises `CycleInWorkflowError` (a `ValueError` subclass exposing `.nodes`) when the graph is cyclic, so a typical GUI flow runs `validate()` first and only calls `plan()` when no `cycle` error was reported.

**Cache invalidation:**

```python
cleared: set[str] = workflow.invalidate(["segmenter_1"])               # cascades to downstream
cleared = workflow.invalidate(["segmenter_1"], cascade=False)          # only this node
```

`invalidate` removes the per-node cache directory under `storage_path` for each named node; with `cascade=True` (default) it also clears every transitively downstream node so a subsequent `compute()` recomputes everything that depended on the changed node. Returns the set of node names whose directories were actually removed (a node with no prior cache is not in the result). Raises `KeyError` for unknown names. **Not safe** to call concurrently with `compute()` on the same workflow — coordinate externally (cancel + join + invalidate).

**Workflow validation:**

```python
errors: list[ValidationError] = workflow.validate()
```

`validate()` runs, in order:

1. Cycle detection (one error per detected cycle).
2. Type compatibility on every column binding (`check_compatibility` between upstream output and downstream input).
3. Missing-required-input check (fields with no binding, no constant, and no `Inputs` default).
4. Pydantic validation of every node's supplied constants (`parameter_invalid` for each violation — this step is opt-in; constants are not Pydantic-validated during `Node.__init__`).
5. Recursive validation of sub-workflows (internal errors carry a `path` prefixed with the parent's name).

Steps 1–3 are already enforced by `Node.__init__` during ordinary graph construction; `validate()` exists so GUIs that built the workflow via `capture_errors()` / `from_dict(validate_only=True, partial=True)` can re-check after the fact. Step 4 only runs here — it is intentionally not performed at construction time, so a GUI editing one field at a time does not need every other field to be valid yet. Callers that previously relied on the engine's best-effort constant coercion at execution time should add explicit defaults or broaden their `Inputs` type annotations.

The module-level helper `bioimageflow.validate_parameters(tool_class, parameters)` validates a single node's constants in isolation (no Workflow needed) — useful for inline GUI form validation.

**Error capture:**

```python
wf = Workflow()
with wf, wf.capture_errors() as errors:
    BadTool()(input=upstream["nonexistent"])
# errors: list[ValidationError]; wf is partially wired.
```

`capture_errors()` is a context manager that redirects node-construction failures (`BindingError`, `ColumnNotFoundError`, unknown kwargs, missing required inputs) into a `ValidationError` list instead of raising. Node registration is best-effort: failed nodes remain registered so that downstream references can still be inspected. Nested blocks push independent buffers.

**Pre-execution planning:**

```python
from bioimageflow import NodePlan, NodePlanStatus
plan: dict[str, NodePlan] = workflow.plan()
for name, entry in plan.items():
    print(name, entry.sig_hash, entry.status)
    # entry.status is one of: CACHED, OUT_OF_DATE, UNEXECUTED, SKIPPED
    # entry.cached and entry.skipped are kept as boolean shortcuts
```

`plan()` returns every node's signature hash and cache status without executing anything. The hashes are byte-identical to what `compute()` would compute for the same nodes — callers can rely on this to report cache state without reimplementing signature composition. `plan()` never launches a Wetlands environment and is safe to call even when `use_wetlands=True`. It raises `CycleInWorkflowError` if the graph is cyclic.

Sub-workflow internal nodes appear under scoped names (`"subworkflow_name/internal_name"`), matching `compute_steps()`. A sub-workflow's outer entry aggregates: `CACHED` only when every internal node is `CACHED`, otherwise `UNEXECUTED`.

### 4.4 Progress Monitoring

Workflows provide a callback-based progress mechanism for monitoring long-running executions:

```python
from bioimageflow import Workflow, ProgressEvent

def on_progress(event: ProgressEvent):
    print(f"[{event.node_name}] {event.status} — row {event.row}/{event.total_rows}")

workflow = Workflow(on_progress=on_progress)
# ... build graph ...
results.compute()
```

`ProgressEvent` reports:

| Field         | Type            | Description                                            |
|--------------|-----------------|--------------------------------------------------------|
| `node_name`   | `str`           | Name of the node being executed                        |
| `status`      | `str`           | One of: `"started"`, `"row_progress"`, `"row_complete"`, `"completed"`, `"cached"`, `"failed"`, `"cancelled"` |
| `row`         | `int`           | Current row index (for `row_complete` and `row_progress`) |
| `total_rows`  | `int`           | Total number of rows for this node                     |
| `message`     | `str \| None`   | Sub-row progress message from `RemoteTaskHandle.update()` |
| `current`     | `int \| None`   | Sub-row progress current value                         |
| `maximum`     | `int \| None`   | Sub-row progress maximum value                         |
| `timestamp`   | `float`         | Unix timestamp                                         |

When using branch-level parallelism, progress events from concurrent nodes may interleave. The engine serializes all `on_progress` callback invocations via an internal lock, so the callback does not need to be thread-safe.

### 4.5 Environment Configuration

`Workflow.get_environment()` provides access to per-environment launch configuration. It accepts a tool instance, an `EnvironmentSpec`, or an environment name string.

```python
wf = Workflow(max_workers=4)

segmenter = CellposeSegmenter()
wf.get_environment(segmenter).max_workers = 2
wf.get_environment(segmenter).worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}
```

Multiple calls with tools sharing the same environment return the same `WorkflowEnvironment` object. Configuration set via one tool applies to all tools in that environment.

**`WorkflowEnvironment`:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | — | Environment name (read-only) |
| `spec` | `EnvironmentSpec` | — | The environment specification (read-only) |
| `max_workers` | `int` | `0` | Number of worker processes. `0` = use `Workflow.max_workers`. |
| `worker_env` | `Callable[[int], dict] \| None` | `None` | Per-worker environment variables. `None` = auto-infer from `ResourceSpec`. |
| `worker_timeout` | `float \| None` | `None` | Inactivity timeout in seconds. When set, Wetlands' health monitor marks the active task as `FAILED` and replaces the worker if it sends no IPC message within this duration — useful for catching native-code deadlocks or segfaults that don't close the pipe. The engine adds its own safety timeout of `max(worker_timeout * 1.5, worker_timeout + 60)` to `task.wait_for()`; if that fires, `WorkerTimeoutError` is raised. `None` = no timeout. |

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
4. **Caching is unaffected** — the `enabled` flag is not part of the signature hash. Re-enabling a node with the same parameters hits the existing cache.
5. **Return value** — `compute()` returns results only for target nodes that were actually executed:
   - If all targets are disabled or have disabled upstreams, `DisabledNodeError` is raised.
   - If some targets are disabled in a multi-target call, only executed targets appear in the returned dict.

#### Step-by-Step Execution (`compute_steps`)

When using `compute_steps()`, skipped nodes are still yielded so the GUI can display them (e.g., grayed out). Each `NodeStep` exposes a `skipped` property:

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
  "tool_module": "my_tools.segmenter",
  "tool_class": "Segmenter",
  "tool_package": "my_tools",
  "tool_package_version": "1.0.0",
  "constants": {"diameter": {"__type__": "float", "value": 30.0}},
  "enabled": false
}
```

`tool_module` stores the **canonical** module path (not the scoped `__1_0_0` variant). When `tool_package` and `tool_package_version` are present, `Workflow.load()` uses `load_versioned_package()` to load the package and `resolve_tool_class()` to find the class in the scoped namespace. When these fields are absent or `null`, the loader falls back to `importlib.import_module()` for backwards compatibility with non-versioned tools.

`Workflow.load()` restores the flag: disabled nodes remain disabled in the loaded workflow.

### 4.7 WorkflowSession (Incremental Editing API)

`WorkflowSession` is a parallel, **dict-backed** model of a workflow designed for GUI clients that mutate the graph incrementally. The session is the canonical state — a `Workflow` is materialized on demand and cached across edits, with selective rebuilds triggered only by structural changes.

**Why a separate class?** `Workflow` builds nodes eagerly in `__init__`, with `_upstream_nodes` and column bindings wired at construction. Retrofitting incremental mutation onto that model would require invasive changes to `Node`. A dict-backed session, materialized to a `Workflow` only when needed, is both simpler and matches what GUIs actually want to send over the wire.

```python
from bioimageflow import WorkflowSession

s = WorkflowSession(data)             # `data` is the wire format from Workflow.to_dict()
s = WorkflowSession.from_dict(data)   # equivalent classmethod

# Mutations
s.add_node({"name": "load", "tool_module": "...", "tool_class": "...",
            "constants": {...}, "args": []})
s.remove_node("load")                  # also strips edges that touch it
s.add_edge({"id": "e1", "from": "load", "to": "seg",
            "column": "path", "field": "input_image"})
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
plan = s.plan()                        # cached across non-structural edits
```

**Edit semantics.** Edits are split into two categories:

- **Structural edits** (`add_node`, `remove_node`, `add_edge`, `remove_edge`) invalidate the cached `Workflow` — the next `to_workflow()` call rebuilds.
- **Non-structural edits** (`set_constant`, `set_enabled`) update the cached `Workflow`'s node fields **in place**. A `set_constant` followed by `validate()` / `plan()` does not re-resolve any tool class — this is the contract that makes the session viable for keystroke-rate validation.

`to_workflow()` always uses `Workflow.from_dict(validate_only=True, partial=True, auto_install=False)`, so per-node failures surface in `wf.failed_nodes` rather than raising. Callers should treat `s.failed_nodes` and `s.is_partial` (via the cached workflow) as part of normal operation, not as exceptional state.

**Round-trip identity.** `WorkflowSession(data).to_dict()` preserves the wire format including edge `id` keys, constant envelopes, and the `enabled` flag (the latter is omitted when re-enabling, matching `Workflow.to_dict`'s clean form).

---

## 5. Execution

### 5.1 The Serialization Boundary

The system has two distinct execution contexts with a strict serialization boundary. `ProcessingTool` spans both contexts; `DataFrameTool` runs entirely in the main process.

| Aspect          | Orchestrator (Main Process)                            | Worker (Wetlands Environment)                          |
|----------------|--------------------------------------------------------|--------------------------------------------------------|
| **Role**        | Planning, scheduling, data management, DataFrameTool execution | Executing ProcessingTool logic                         |
| **Packages**    | `bioimageflow` + `bioimageflow-core` + Pandas + Pydantic + graph lib | `bioimageflow-core` (zero deps) + tool dependencies    |
| **State**       | Holds the DataFrame, graph, cache                      | Runtime state allowed (e.g., cached model instances)   |
| **Data in/out** | `list[dict]` sent and received via Wetlands            | `list[dict]` received and sent via Wetlands            |

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
   3. **Cache Check:** Compute the [signature hash](#61-signature-hash). If a cache hit exists, load cached results and skip to step 6.
   4. **Merge:** Call `tool.merge_dataframes(dfs, arguments)`. Default: inner join on index.
   5. **Transform:** Call `tool.transform(df, arguments)`. Returns a (potentially different) DataFrame. Default: identity (passthrough).
   6. **Caching:** Save the result DataFrame and metadata to the [storage structure](#72-directory-structure).

#### ProcessingTool Execution Path

   1. **Index Alignment:** Collect all upstream nodes referenced via column bindings. Compute the aligned index — the finest-grained index that is compatible with all upstream indices (see [Section 5.3](#53-dataframe-semantics)). If upstream indices are incompatible (no common lineage), raise `IndexAlignmentError`.
   2. **Value Resolution:** For each row in the aligned index, materialize input values from the column bindings. The orchestrator validates resolved values using Pydantic models built from the tool's `IOModel` declarations. Path-typed values are converted to absolute runtime paths in the orchestrator.
   3. **Output Templating:** Resolve output path templates for every row (see [Section 7.1](#71-output-templating-engine)). The main process must resolve output paths *before* dispatch since the worker has no knowledge of workflow graph state. Generated output paths are absolute and point under the run's `assets/` directory.
   4. **Cache Check:** Compute the [signature hash](#61-signature-hash). If a cache hit exists, load cached results and skip to step 10.
   5. **Execution Context:** Create the timestamp/hash run directory and its `assets/`, shared `work/`, `work/rows/`, per-row `work/rows/<safe_row_id>/`, and `work/batch/` children. Build one picklable `ExecutionContext` per input row, plus one batch context. Every context shares the same `work_dir` (`run_dir/work/`) and `rows_dir` (`run_dir/work/rows/`); row contexts receive a private `row_dir`, and the batch context receives a private `batch_dir`. Context paths are runtime details and are not included in the signature hash.
   6. **Serialization:** Convert resolved values to `list[dict]` (one dict per row, containing all resolved input values and output paths). When a tool declares `context`, serialize the corresponding `ExecutionContext` separately from `Arguments`.
   7. **Environment Launch:** If not already running, create/reuse the Wetlands environment. If an environment with the same name already exists but its dependency hash differs, raise `EnvironmentMismatchError`.
   8. **Dispatch:** If `process_batch` was overridden, submit a single batch call via `env.submit()`. Otherwise, submit all `process_row` calls via `env.map_tasks()`. When `max_workers > 1`, rows execute in parallel across worker processes. When `max_workers == 1` (default), rows execute sequentially in a single worker (equivalent to the previous behavior). Results are always collected in submission order to preserve deterministic DataFrame construction.
   8b. **Output Validation (worker-side):** After `process_row`/`process_batch` returns, the worker performs lightweight `isinstance` checks on each output field against the tool's `Outputs` annotations (e.g., image path fields must be `Path` or `str`, `int` fields must be `int`). These checks use only the standard library (no Pydantic) and add negligible overhead. Errors are raised immediately in the worker with clear stack traces pointing to the tool code.
   9. **DataFrame Construction:** Build the output DataFrame from the tool's results. The output contains **only** the columns declared in `Outputs` (no upstream columns are carried forward). The index is preserved from the aligned input index, with explosion for 1-to-N outputs (see Section 5.3). This DataFrame is the node's graph-level output and may be passed as a positional upstream input to a `DataFrameTool`; individual declared columns remain addressable through `ColumnRef` bindings.
   10. **Caching:** Save the result DataFrame and metadata to the [storage structure](#72-directory-structure).

#### Orchestrator-Worker Interaction (ProcessingTool Steps 6-10)

The orchestrator drives all calls into the environment using Wetlands' Task API (`env.submit()` and `env.map_tasks()`). The tool's file path and class name are passed so the worker can instantiate the tool.

```python
# === Orchestrator (main process) ===

# 7. Environment Launch
env = env_manager.get_or_create(tool.environment)
# env.launch() was already called with configured max_workers

# 8-9. Dispatch
worker_file = "bioimageflow_core/worker.py"  # absolute path resolved at runtime
tool_class_name = type(tool).__name__
tool_file_path = _find_tool_file(type(tool))  # resolved via env_manager

if has_batch:
    # Single task for the whole batch
    task = env.submit(worker_file, "run_process_batch",
                      args=(tool_file_path, tool_class_name,
                            arguments_dicts, batch_context.to_dict()))
    task.wait_for()
    results = task.result
else:
    # One task per row — parallel when max_workers > 1
    row_args = [(tool_file_path, tool_class_name, d, c.to_dict())
                for d, c in zip(arguments_dicts, row_contexts)]
    tasks = env.map_tasks(worker_file, "run_process_row", row_args)
    for task in tasks:
        task.wait_for()
    # task.result is already list[dict] (one dict per output row)
    results = [task.result for task in tasks]

# 9. DataFrame Construction (outputs only — no upstream column carry-forward)
# Deterministic row-expansion algorithm:
# - Iterate aligned input indices in order.
# - Preserve worker output order for each row.
# - Output DataFrame contains ONLY the tool's Outputs fields (no input columns).
# - If a row has one output: keep original index.
# - If a row has N>1 outputs: create child indices "<parent>::0", "<parent>::1", ..., "<parent>::N-1".
#   The '::' sequence is reserved as the explosion separator (see Section 5.3).
# - Build a new DataFrame from output rows only.
expanded = []
for i, row_outputs in enumerate(results):
    parent_index = aligned_index[i]
    if len(row_outputs) == 1:
        expanded.append((parent_index, row_outputs[0]))
    else:
        for j, output in enumerate(row_outputs):
            expanded.append((f"{parent_index}::{j}", output))
node_df = pandas.DataFrame([r for _, r in expanded], index=[idx for idx, _ in expanded])
```

```python
# === Worker (inside Wetlands environment) ===
# This module is loaded via env.import_module(tool.__module__).
# The worker discovers tool classes by scanning the module for BaseTool subclasses.

from bioimageflow_core import Arguments, BaseTool
import inspect

def _discover_tools(module):
    """Build a name→class registry from all BaseTool subclasses in the module."""
    registry = {}
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseTool) and obj is not BaseTool and hasattr(obj, 'name'):
            registry[obj.__name__] = obj
    return registry

# Built lazily on first call; maps class name → tool class
_tool_registry = None
_instances = {}  # Cache tool instances (e.g., to keep GPU models loaded)

def _get_instance(tool_class_name):
    global _tool_registry
    if _tool_registry is None:
        import sys
        _tool_registry = _discover_tools(sys.modules[__name__])
    if tool_class_name not in _instances:
        _instances[tool_class_name] = _tool_registry[tool_class_name]()
    return _instances[tool_class_name]

def run_process_batch(tool_class_name, arguments_dicts, context_dict=None):
    tool = _get_instance(tool_class_name)
    args_list = [Arguments(**d) for d in arguments_dicts]
    kwargs = {}
    if context_dict is not None and _accepts_context(tool.process_batch):
        kwargs["context"] = ExecutionContext.from_dict(context_dict)
    results = tool.process_batch(args_list, **kwargs)
    # Auto-wrap list[Outputs] → list[list[Outputs]] for 1-to-1 batch tools
    if results and not isinstance(results[0], list):
        results = [[r] for r in results]
    return [[vars(out) for out in row_outputs] for row_outputs in results]

def run_process_row(tool_class_name, arguments_dict, context_dict=None):
    tool = _get_instance(tool_class_name)
    args = Arguments(**arguments_dict)
    kwargs = {}
    if context_dict is not None and _accepts_context(tool.process_row):
        kwargs["context"] = ExecutionContext.from_dict(context_dict)
    result = tool.process_row(args, **kwargs)
    # Normalize: single Outputs → list
    outputs = result if isinstance(result, list) else [result]
    return [vars(out) for out in outputs]
```

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

## 6. Hashing, Caching, and Provenance

### 6.1 Signature Hash

Before execution, every node computes a signature hash:

```
SHA256(tool_name + tool_version + env_dependencies_hash + JSON(resolved_parameters) + upstream_hashes)
```

Where:
- `tool_name`: The tool's `name` attribute.
- `tool_version`: For tools loaded from the tool store, the stamped `_bif_package_version` (e.g., `"1.0.0"`). For tools installed as regular packages, the version from `importlib.metadata`. For tools not distributed as packages, the engine uses the source file's modification time. Falls back to `"unversioned"` in interactive/REPL contexts. This ensures that different versions of the same tool produce different cache keys.
- `env_dependencies_hash`: SHA256 of the normalized `EnvironmentSpec.dependencies` (see [Section 3.1](#31-environmentspec)). Empty string for `DataFrameTool` (no environment). This ensures that changing a tool's environment (e.g., `cellpose==3.0` → `cellpose==4.0`) invalidates the cache.
- `resolved_parameters`: All resolved input values (constants and column mappings), serialized deterministically via a custom serializer:

```python
def deterministic_serialize(obj: Any) -> str:
    """Serialize an object deterministically for hashing.
    Handles known types explicitly; raises TypeError on unknown types
    to prevent silent non-deterministic coercion.
    """
    def _default(o):
        if isinstance(o, Path):
            return o.as_posix()  # Always POSIX — consistent across OSes
        if isinstance(o, (set, frozenset)):
            return sorted(str(x) for x in o)  # Deterministic ordering
        if isinstance(o, tuple):
            return list(o)
        if isinstance(o, Enum):
            return o.value
        if hasattr(o, '__dataclass_fields__'):  # Frozen dataclasses (SharedArray, ImageSpec)
            return {k: getattr(o, k) for k in o.__dataclass_fields__}
        raise TypeError(
            f"Cannot serialize {type(o).__name__} for hashing. "
            f"Add explicit handling in deterministic_serialize()."
        )
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)
```

- `upstream_hashes`: The signature hash(es) of all upstream nodes, sorted alphabetically by node name to ensure deterministic ordering.

If the hash matches an existing cached result, the node is skipped and cached results are loaded.

### 6.2 Development Mode

In development mode (`workflow.compute(dev_mode=True)`), the hash formula additionally includes the **source hash** of the tool class:

```
SHA256(tool_name + tool_version + env_dependencies_hash + source_hash + JSON(resolved_parameters) + upstream_hashes)
```

Where `source_hash` is `SHA256(inspect.getsource(tool_class))`. This auto-invalidates caches when tool code changes, without requiring a version bump. Development mode is intended for iteration; production workflows should rely on version-based hashing for reproducibility.

The same hashes are exposed pre-execution via [`Workflow.plan()`](#65-pre-execution-planning); callers that need to report cache status without executing the workflow should use `plan()` rather than reimplementing hash composition.

### 6.3 Limitations

- **Path-based, not content-based:** The hash includes file *paths*, not file *contents*. If an input file is modified without changing its path, the cache will report a false hit. Users can manually invalidate the cache when needed.
- **Transitive dependency changes:** The `env_dependencies_hash` catches version spec changes (e.g., `cellpose==3.0` → `cellpose==4.0`). However, if a dependency releases a bug fix *without* changing the pinned version (e.g., a Conda rebuild of `cellpose==3.0`), the cache will not invalidate. Bump the tool's package version or use `dev_mode` to force re-execution.

### 6.4 Cache Retention Policy

Users configure cache retention per workflow:

- **`max_executions`** (default: `0`): Number of past executions to keep. `0` means result files are deleted when a new execution completes. Higher values (e.g., `3`, `100`) retain that many historical results.
- **`max_age`** (optional): Maximum age for cached results. Results older than this are eligible for deletion. Cleanup runs at workflow execution time, or can be triggered via a dedicated cleanup function.

### 6.5 Pre-execution Planning

`Workflow.plan()` exposes the same signature hashes (byte-identical) at pre-execution time, along with each node's cache-hit status — without actually executing anything. Callers that need to report cache status (for example, a GUI indicating "cached / out-of-date / unexecuted" per node) should use `plan()` rather than reimplementing hash composition.

```python
from bioimageflow import NodePlan, NodePlanStatus
plan: dict[str, NodePlan] = workflow.plan(dev_mode=False)
for name, entry in plan.items():
    assert isinstance(entry, NodePlan)
    # entry.node_name, entry.sig_hash, entry.status, entry.upstream
    # entry.cached / entry.skipped: boolean shortcuts derived from status
```

`NodePlan` is a frozen dataclass with fields `node_name`, `sig_hash`, `status` (a `NodePlanStatus`), and `upstream` (tuple of upstream scoped names). The `cached` and `skipped` booleans are read-only shortcuts (`cached == status is CACHED`, `skipped == status is SKIPPED`). `NodePlanStatus` values:

| Status | Meaning |
|--------|---------|
| `CACHED` | Current sig_hash matches an existing cache entry; `compute()` would short-circuit. |
| `OUT_OF_DATE` | Cache directory has prior runs but none match the current sig_hash; `compute()` would re-execute. |
| `UNEXECUTED` | No cache directory yet — node has never run. |
| `SKIPPED` | Node is disabled, or its upstream chain contains a disabled node. `sig_hash` is empty. |

Sub-workflow internal nodes appear under scoped names `"subworkflow_name/internal_name"`. The outer sub-workflow entry's status is `CACHED` only when every internal node is `CACHED`, otherwise `UNEXECUTED`. `plan()` never launches a Wetlands environment (an internal non-Wetlands engine is used regardless of `Workflow.use_wetlands`). It raises `CycleInWorkflowError` (a `ValueError` subclass exposing `.nodes: list[str]`) on a cyclic graph; call `workflow.validate()` first if a cycle is possible.

---

## 6.6 Validation Error Reference

`ValidationError` is a frozen dataclass produced by:

- `Workflow.capture_errors()` — for construction-time errors when the context is active.
- `Workflow.from_dict(..., partial=True)` — for tool-resolution and per-node construction failures during deserialization (also accessible via `wf.errors` and `wf.failed_nodes` after the call).
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
    path: tuple[str, ...] = ()                      # sub-workflow scope, root → leaf
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

**Template declaration rule:** Templates must be declared explicitly with `Template("...")` on fields whose type annotation is Path-based (`Path` or `Annotated[Path, ...]`). Non-path fields cannot declare `Template(...)`. Old-style explicit template defaults such as `"{input_image.stem}.tif"` or `Path("{input_image.stem}.tif")` are invalid and raise an error.

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

The library runtime storage layout below is rooted at `Workflow.storage_path`.
When the platform runs a saved workflow, it sets that root from the active
workspace: `workspace/outputs/<workflow_id>/`. The workspace itself also
contains the saved workflow tree and workspace-owned custom tools:

```text
workspace/
  workflows/
    <folder>/<workflow>/workflow.json
  tools/
  data/
  outputs/
    <workflow_id>/
      ... runtime storage layout below ...
```

```text
/workflow_storage_root/
  ├── data/
  │   └── <node_name>/
  │       └── <YYYYMMDD_HHMMSS>_<hash_12chars>/
  │           ├── metadata.json     # Tool version, timestamp, user
  │           ├── parameters.json   # Resolved configuration
  │           ├── dataframe.csv     # Output table
  │           ├── assets/           # Declared output files (ProcessingTool only)
  │           │   ├── img1_seg.tif
  │           │   └── img2_seg.tif
  │           └── work/             # Runtime scratch/intermediate files
  │               ├── rows/
  │               │   └── <safe_row_id>/
  │               └── batch/
  └── provenance_graph.json         # Full DAG dump
```

The hash directory name is prefixed with a creation timestamp (`YYYYMMDD_HHMMSS`) for easy chronological sorting, followed by the first 12 characters of the signature hash (e.g., `20260309_143022_a1b2c3d4e5f6`). Cache lookup matches directories by the trailing hash suffix.

`assets/` is the only directory for files that are part of a tool's declared `Outputs`. `work/` is reserved for files that exist only to execute the tool: temporary images, implicit files created by external CLIs, unpacked models, and similar intermediates. Files in `work/` are not part of the tool output contract, are not exposed as DataFrame outputs unless a tool explicitly returns them, and are not included in the signature hash.

Package-local `data/` directories are read-only static resources shipped with the tool package. Tools must not generate or mutate files in package `data/` at runtime. If a static resource is missing in a development checkout and must be generated as a fallback, the tool generates it under a tool-named child of `ExecutionContext.work_dir`.

External command wrappers must avoid process-CWD pollution. If a row-level binary writes implicit files such as `LoG.tif`, the wrapper passes `cwd=context.row_dir` to `subprocess.run()` or equivalent. Batch-level wrappers use `cwd=context.batch_dir`. Shared generated runtime resources go under `context.work_dir`, preferably in a tool-named child directory. The engine does not change the process working directory globally.

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

Both helpers use `numpy` and `multiprocessing.shared_memory` at runtime (not declared as dependencies). This is safe because only tools that process image arrays call these functions, and those tools always have numpy in their environment.

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
- **Garbage Collection:** The engine performs topology-aware GC. A shared memory segment is unlinked when all direct downstream consumers in the DAG have completed successfully, or the row containing the reference is filtered out by all downstream branches.
- **Crash Safety:** The engine registers an `atexit` handler that cleans up all tracked `bif_*` segments on abnormal termination. A CLI utility (`bioimageflow clean-shm`) is provided to manually wipe orphaned segments left by hard crashes (SIGKILL, OOM).
- **Persistence:** Shared memory is volatile. If caching is requested for a node that produced shared memory outputs, the engine automatically dumps the segment to disk. When the node is subsequently loaded from cache, the engine automatically reads the file back into a new `SharedArray` before dispatching to downstream tools, thereby strictly respecting the `ImageShared` interface contract.

When a tool stores a `SharedArray` in an output DataFrame column, it carries the shared memory name, array shape, and dtype — sufficient for downstream tools to attach and read the data. Since `SharedArray` is a frozen dataclass defined in `bioimageflow-core`, it is picklable and can cross the serialization boundary.

---

## 9. Error Handling

- **Binding errors** (`BindingError`): Raised at graph construction time when a required input field has no source (no column reference, no constant, no default). Lists the missing field and available sources.
- **Column not found** (`ColumnNotFoundError`): Raised at graph construction time when a column reference (`node["col"]` or node shorthand) refers to a column that does not exist in the upstream node's output schema. Includes available columns and close-match suggestions. For DataFrameTool upstreams without `Outputs`, this check is deferred to execution time.
- **Index alignment errors** (`IndexAlignmentError`): Raised at execution time when a ProcessingTool references columns from upstream nodes whose indices have no common lineage. The user must insert a merge DataFrameTool to combine the data explicitly.
- **Template errors**: Raised at graph construction time if a ProcessingTool output template references undefined variables or input fields.
- **Worker exceptions:** Exceptions raised in `process_row` or `process_batch` are captured by Wetlands and re-raised in the main process with the original stack trace.
- **DataFrameTool exceptions:** Exceptions raised in `merge_dataframes` or `transform` propagate directly since they run in the main process.
- **Disabled node errors** (`DisabledNodeError`): Raised at execution time when all requested target nodes are disabled or have disabled upstream dependencies. When only some targets are disabled in a multi-target `compute()` call, the disabled targets are silently omitted from the result dict.
- **Row-level failure:** When a single row fails in `process_row`, the entire node execution fails. The engine does not produce partial results.

---

## 10. Resource Constraints

Processing tools can declare their resource requirements via an optional `ResourceSpec`. Declarations are engine-agnostic — each execution engine interprets them according to its own scheduling model.

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
```

- `ResourceSpec.max_concurrent` is reserved for the Parsl parallel engine and is not used by the DefaultEngine.
- The **parallel engine (Parsl)** maps resource specs to its executor model — e.g., `gpu=1` routes to a GPU executor pool, `max_concurrent=4` limits concurrent task submissions.
- Tools without `resources` have no constraints (unlimited concurrency, CPU-only).

`ResourceSpec` lives in `bioimageflow_core.environment` alongside `EnvironmentSpec`.

**DefaultEngine worker resolution:** The DefaultEngine determines `max_workers` per environment using a three-level approach:

1. **Explicit override:** `wf.get_environment(tool).max_workers = M` takes precedence.
2. **GPU auto-inference:** If any tool in the environment declares `ResourceSpec(gpu >= 1)` and no explicit `worker_env` was set, the engine auto-generates `worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}`.
3. **Workflow default:** `Workflow(max_workers=N)` provides the baseline for all environments.

**GPU assignment:** When `ResourceSpec.gpu >= 1`, the DefaultEngine automatically assigns `CUDA_VISIBLE_DEVICES` per worker process: worker `i` gets `CUDA_VISIBLE_DEVICES=str(i)`. This default can be overridden by providing an explicit `worker_env` via `get_environment()`.

**Explicit override:**
```python
wf = Workflow(max_workers=4)
wf.get_environment(my_gpu_tool).worker_env = lambda i: {
    "CUDA_VISIBLE_DEVICES": str(i),
    "OMP_NUM_THREADS": "4",
}
```

---

## 11. Logging

BioImageFlow uses Python's standard `logging` module with node-specific logger names.

```python
import logging

# Framework-level logger
logger = logging.getLogger("bioimageflow")

# Per-node loggers (created by the engine during execution)
node_logger = logging.getLogger(f"bioimageflow.node.{node_name}")
```

- The execution engine creates a `FileHandler` per execution run that saves logs to the workflow's provenance directory.
- A `JsonFormatter` is available for machine-readable output (structured event logging).
- Worker-side log messages are forwarded to the main process via the Wetlands communication channel, tagged with the node name and row index.
- Log levels follow standard Python conventions: `DEBUG` for per-row details, `INFO` for node lifecycle events, `WARNING` for compatibility warnings (e.g., unverified type constraints), `ERROR` for failures.

---

## 12. Parallelism

- **Default engine (`DefaultEngine`):** Executes nodes in topological order. Independent nodes (nodes on different DAG branches whose dependencies are all satisfied) execute concurrently using threads. `ProcessingTool` nodes are dispatched to Wetlands workers (which run in separate processes), so the GIL is not a bottleneck. `DataFrameTool` nodes always execute in the main thread with a lock, since they operate on DataFrames in the main process and may not be thread-safe. Within each node, `process_row` calls are dispatched via `env.map_tasks()`. When the effective `max_workers > 1`, rows run in parallel across Wetlands worker processes. When `max_workers == 1` (default), rows run sequentially.
- **Sequential engine (`SequentialEngine`):** Subclass of `DefaultEngine` that forces single-worker, single-node-at-a-time execution. Useful for debugging and deterministic reproduction.
- **Parallel engine (Parsl):** For distributed execution across clusters. Will be implemented later. Uses `ResourceSpec` declarations (see [Section 10](#10-resource-constraints)) to route tasks to appropriate executors.

The choice of engine is transparent to tool authors — the same tool code works with both.

---

## 13. Cancellation

Workflow execution can be cancelled cooperatively from another thread.

```python
import threading

wf = Workflow(on_progress=my_callback)
# ... build graph ...

thread = threading.Thread(target=lambda: wf.compute(target_node))
thread.start()

# Later, from the main thread or a GUI button:
wf.cancel()
thread.join()
```

**Cancellation semantics:**

1. `Workflow.cancel()` sets an internal flag checked by the engine.
2. The engine checks the flag before dispatching each node. If set, it raises `WorkflowCancelledError`.
3. For in-flight Wetlands tasks (rows currently being processed), the engine calls `task.cancel()` on each. The remote function can check `task.cancel_requested` to exit early.
4. Cancellation is cooperative: tools that don't check `task.cancel_requested` will finish their current row, but no new rows are dispatched.
5. After cancellation, `compute()` raises `WorkflowCancelledError`. Environments are still shut down properly.
6. `DataFrameTool` nodes run in the main thread and cannot be interrupted mid-execution. The cancel flag is checked before dispatching each node (including DataFrameTool nodes), so a long-running `transform()` will complete but no further nodes are dispatched.

**Tool-side cooperative cancellation:**

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

Tools that don't check `task.cancel_requested` are unaffected — they complete normally and the engine simply stops dispatching further rows/nodes.

**Cancelled task handling:** The engine relies solely on `task.status == CANCELED` to detect cancellation and never accesses `task.result` for cancelled tasks. The return value of a cancelled tool is irrelevant — the engine skips result collection for that row.

**Cancellation scoping:** `cancel()` is scoped to the current `compute()` execution. The cancel flag is cleared at the start of each `compute()` call. Calling `cancel()` when no execution is running has no effect.

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
)
from bioimageflow_core.io import load_image, save_image
from bioimageflow_core.shm import create_shared_output, open_shared_array

# === bioimageflow (main process only) ===
from bioimageflow import (
    DataFrameTool, Passthrough,
    Workflow,
    SubWorkflow,
    # Built-in merge tools
    InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect,
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

## 14. Sub-Workflows

Sub-workflows allow users to package an entire workflow DAG as a reusable node. A `SubWorkflow` encapsulates an internal DAG with declared inputs and outputs, and behaves like a single node in the parent workflow.

### 14.1 SubWorkflow Definition

*Module: `bioimageflow.sub_workflow`*

`SubWorkflow` is a new base class in the `bioimageflow` package (orchestrator-only — not in `bioimageflow-core`). It is **not** a subclass of `BaseTool`; it is a standalone callable that produces a `SubWorkflowNode`.

```python
from bioimageflow.sub_workflow import SubWorkflow
from pathlib import Path
from typing import Annotated

from bioimageflow_core import IOModel, ImageSpec, Semantic, Arguments

class SegmentAndMeasure(SubWorkflow):
    name = "segment_and_measure"

    class Inputs(IOModel):
        image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        cell_count: int
        mean_intensity: float

    def build(self, inputs):
        """Build the internal DAG.

        Args:
            inputs: A SubWorkflowInputProxy providing ColumnRef-like handles
                    for each declared input field.

        Returns:
            A dict mapping output field names to ColumnRefs from internal nodes.
        """
        segment = CellposeSegmenter()
        measure = MeasureStats()

        masks = segment(input_image=inputs.image, diameter=inputs.diameter)
        stats = measure(image=inputs.image, mask=masks["mask"])

        return {
            "mask": masks["mask"],
            "cell_count": masks["cell_count"],
            "mean_intensity": stats["mean_intensity"],
        }
```

**SubWorkflow class attributes:**

| Attribute  | Type               | Description                                   |
|-----------|-------------------|-----------------------------------------------|
| `name`     | `str`              | Unique identifier for the sub-workflow         |
| `Inputs`   | `IOModel subclass` | Declared inputs (exposed to parent workflow)   |
| `Outputs`  | `IOModel subclass` | Declared outputs (exposed to parent workflow)  |

**Concrete `SubWorkflow` subclasses must:**
- Declare `name`, `Inputs`, and `Outputs` as class attributes.
- Override `build(self, inputs)` → `dict[str, ColumnRef]` mapping each `Outputs` field to an internal node column.

### 14.2 Using a Sub-Workflow

From the parent workflow's perspective, a `SubWorkflow` is called like any other tool — keyword arguments bind to `Inputs`, and the returned node exposes `Outputs` columns:

```python
seg_measure = SegmentAndMeasure()

with Workflow(storage_path="./results") as wf:
    raw = load_images(path="./data")
    results = seg_measure(image=raw["path"], diameter=25.0)

    # Access outputs like any other node
    export = save(mask=results["mask"], stats=results["mean_intensity"])
    wf.compute(export)
```

### 14.3 SubWorkflowInputProxy

When `SubWorkflow.__call__()` is invoked, it creates a `SubWorkflowInputProxy` — a lightweight proxy that acts as a virtual source node for the internal DAG. Internal nodes can reference proxy fields via attribute access (`inputs.image`) or subscript (`inputs["image"]`), both of which return `ColumnRef` objects.

The proxy is backed by a real `Node` (with no tool) that the engine replaces with the actual parent-workflow upstream data at execution time.

### 14.4 SubWorkflowNode

`SubWorkflowNode` is a `Node` subclass that represents a sub-workflow in the parent DAG. It holds:

- The `SubWorkflow` definition
- The internal nodes (encapsulated — not registered with the parent workflow)
- Input mappings: parent ColumnRefs/constants → internal proxy fields
- Output mappings: internal node columns → declared `Outputs` fields

`SubWorkflowNode` supports `__getitem__` for output column access: `results["mask"]` returns a `ColumnRef` pointing to the sub-workflow node.

**Internal nodes are not directly accessible from the parent workflow's `nodes` dict.** They are accessible via `sub_workflow_node.internal_nodes` for debugging.

### 14.5 Execution Strategy: Flattening

At execution time, the engine **flattens** the sub-workflow into its constituent internal nodes:

1. When the engine encounters a `SubWorkflowNode`, it expands it into its internal nodes.
2. Input proxy nodes are replaced with direct references to the parent's upstream data.
3. Internal nodes execute normally in topological order, using existing execution paths.
4. After all internal nodes execute, the engine assembles the sub-workflow's output DataFrame by collecting columns from the output mapping.

**Consequences of flattening:**
- **Caching:** Each internal node caches independently (fine-grained).
- **Environment reuse:** Internal `ProcessingTool`s with the same `EnvironmentSpec` as parent-level tools share the same Wetlands environment.
- **Name scoping:** Internal node names are prefixed with the sub-workflow node name: `"segment_and_measure_1/cellpose_segmenter_1"`. Cache directories follow the same scoping.

### 14.6 Debugging with `compute_steps`

Internal nodes are visible during step-by-step execution via `compute_steps()`. Each internal node is yielded as its own `NodeStep` with a scoped name:

```python
for step in wf.compute_steps(results):
    print(f"Next: {step.node_name} (env: {step.environment})")
    step.prepare()     # launches Wetlands env — attach debugger here
    df = step.execute()
```

This yields steps like:
```
Next: file_loader_1 (env: None)
Next: segment_and_measure_1/cellpose_segmenter_1 (env: cellpose)
Next: segment_and_measure_1/stub_stats_1 (env: imageio)
```

### 14.7 Cache Directory Structure

Internal nodes store their cache under the sub-workflow node's directory:

```text
storage_path/data/
├── segment_and_measure_1/
│   ├── cellpose_segmenter_1/
│   │   └── 20260323_.../
│   └── stub_stats_1/
│       └── 20260323_.../
├── file_loader_1/
│   └── 20260323_.../
```

### 14.8 Serialization

`Workflow.export()` serializes `SubWorkflowNode` with its internal structure:

```json
{
  "name": "segment_and_measure_1",
  "type": "sub_workflow",
  "sub_workflow_module": "my_tools.pipelines",
  "sub_workflow_class": "SegmentAndMeasure",
  "sub_workflow_package": "my_tools",
  "sub_workflow_package_version": "1.0.0",
  "constants": {"diameter": {"__type__": "float", "value": 25.0}},
  "input_mapping": {...},
  "output_mapping": {...},
  "internal_nodes": [...],
  "internal_edges": [...]
}
```

`sub_workflow_module` stores the canonical module path. When `sub_workflow_package` and `sub_workflow_package_version` are present, `Workflow.load()` uses `load_versioned_package()` and `resolve_tool_class()` to find the `SubWorkflow` class. When absent, it falls back to `importlib.import_module()`.

`Workflow.load()` reconstructs `SubWorkflowNode` from the serialized form by re-importing and re-calling the `SubWorkflow` class. Because `build()` uses relative imports that resolve within the scoped namespace, the internal tools are automatically from the correct package version.

`Workflow.from_dict` / `Workflow.to_dict` handle `SubWorkflowNode` identically to `Workflow.load` / `Workflow.export` — the dict shape and the file shape are the same.

### 14.9 Nesting

Sub-workflows may contain other sub-workflows. The engine flattens recursively — all internal nodes at every nesting level are expanded into the parent execution graph. Name scoping nests: `"outer_1/inner_1/tool_1"`.

### 14.10 Error Handling

- **Missing output mapping:** If `build()` returns a dict missing a declared `Outputs` field, a `ValueError` is raised at graph construction time.
- **Extra output mapping:** If `build()` returns keys not in `Outputs`, they are ignored with a warning.
- **Input binding errors:** The same `BindingError` rules as `ProcessingTool` apply — missing required inputs with no default raise `BindingError`.
- **Cycle detection:** Cycles involving sub-workflow internals are detected during flattening.

### 14.11 Config-Driven Sub-Workflows

*Module: `bioimageflow.sub_workflow`*

Sub-workflows can be defined declaratively from a JSON-serializable config dict, without writing a Python class. This enables GUI servers and external tools to define sub-workflows at runtime.

#### Factory Method

```python
config = {
    "name": "spot_detection",
    "inputs": {
        "input_image": {"type": "Path", "image_spec": {"semantics": ["intensity"]}},
        "channel": {"type": "int", "default": 0},
    },
    "outputs": {
        "labeled_spots": {"type": "Path", "image_spec": {"semantics": ["label"]}},
        "num_spots": {"type": "int"},
    },
    "nodes": [
        {
            "name": "extract",
            "tool_class": "ExtractChannel",
            "tool_module": "bioimageflow_common_tools",
            "tool_package": "bioimageflow-common-tools",
            "tool_package_version": "0.1.0",
            "inputs": {
                "input_image": {"from_input": "input_image"},
                "channel": {"from_input": "channel"},
            },
        },
        {
            "name": "cc",
            "tool_class": "ConnectedComponents",
            "tool_module": "bioimageflow_common_tools",
            "inputs": {
                "input_image": {"from_node": "extract", "column": "output_image"},
            },
        },
    ],
    "output_mapping": {
        "labeled_spots": {"from_node": "cc", "column": "output_image"},
        "num_spots": {"from_node": "cc", "column": "num_labels"},
    },
}

sw = SubWorkflow.from_config(config)
```

`SubWorkflow.from_config(config)` returns a `_ConfigDrivenSubWorkflow` instance — a `SubWorkflow` subclass that stores the config and implements `build()` by interpreting it declaratively. All existing `SubWorkflow` machinery (`__call__`, `SubWorkflowNode`, flattening, caching, scoped names) is reused without modification.

The config's `inputs` and `outputs` are the published interface of the
sub-workflow. In GUI-created workflows, publishing a parameter adds an entry to
`inputs` and rewrites the internal node field to `{"from_input": ...}`.
Unpublishing removes that entry and restores the internal field to a local
constant or connection. Publishing an output adds an entry to `outputs` and
`output_mapping`; unpublishing removes both entries. Parent workflows only see
these published fields on the outer `SubWorkflowNode`.

#### Config Schema

**Top-level keys:**

| Key              | Type   | Required | Description                                    |
|-----------------|--------|----------|------------------------------------------------|
| `name`           | `str`  | Yes      | Sub-workflow identifier (used for node naming)  |
| `inputs`         | `dict` | Yes      | Input field definitions (may be empty `{}`)     |
| `outputs`        | `dict` | Yes      | Output field definitions                        |
| `nodes`          | `list` | Yes      | Internal node definitions, in dependency order  |
| `output_mapping` | `dict` | Yes      | Maps output fields to internal node columns     |

**Field definition** (in `inputs`/`outputs`):

| Key          | Type   | Required | Description                                       |
|-------------|--------|----------|---------------------------------------------------|
| `type`       | `str`  | Yes      | One of: `"int"`, `"float"`, `"str"`, `"bool"`, `"Path"`, `"ImageFile"` |
| `image_spec` | `dict` or `null` | No | For `"Path"`, a dict wraps the type with `Annotated[Path, ImageSpec(...)]`; missing or `null` leaves it as plain `Path`. For `"ImageFile"`, missing or `null` uses an empty `ImageSpec` and produces `Annotated[Path, ImageSpec()]`. |
| `default`    | any    | No       | Default value for the field                       |

**`ImageFile` alias:** accepted for GUI schema round-trips. It is equivalent to a `Path` field carrying an `ImageSpec` annotation. If `image_spec` is missing or `null`, the annotation uses an empty `ImageSpec`.

**`image_spec` dict:** `{"semantics": [...], "layouts": [...], "dtypes": [...], "formats": [...]}`. Values are lists of enum value strings (e.g., `"intensity"`, `"label"`, `"YX"`). All keys are optional; missing keys mean "any" (empty set). For a `"Path"` field, `image_spec: null` is treated the same as a missing `image_spec` key and does not create an image annotation.

**Node definition:**

| Key                    | Type   | Required | Description                                  |
|-----------------------|--------|----------|----------------------------------------------|
| `name`                 | `str`  | Yes      | Internal node name (unique within config)    |
| `tool_class`           | `str`  | Yes*     | Tool class name                              |
| `tool_module`          | `str`  | Yes*     | Python module containing the tool            |
| `tool_package`         | `str`  | No       | Versioned package name (for `resolve_tool_class`) |
| `tool_package_version` | `str`  | No       | Package version                              |
| `type`                 | `str`  | No       | `"sub_workflow"` for nested sub-workflow nodes |
| `config`               | `dict` | No       | Inline config for nested config sub-workflow |
| `sub_workflow_class`   | `str`  | No       | Class name for nested class-based sub-workflow |
| `sub_workflow_module`  | `str`  | No       | Module for nested class-based sub-workflow   |
| `inputs`               | `dict` | Yes      | Input bindings for this node                 |

*Required for tool nodes (when `type` is not `"sub_workflow"`).

**Input reference types** (values in a node's `inputs` dict):

- `{"from_input": "field_name"}` — references a sub-workflow input. Resolves to a `ColumnRef` (if the parent bound a column) or a constant (if default/constant).
- `{"from_node": "node_name", "column": "col_name"}` — references an output column from a previously defined internal node.
- Raw value (`int`, `float`, `str`, `bool`, `list`) — constant binding passed directly to the tool.

**Output mapping** values use only `{"from_node": ..., "column": ...}`.

`SubWorkflow.from_config()` validates the published interface before building
the DAG:

- every `from_input` reference must name a declared config input;
- every declared output must have an `output_mapping` entry;
- `output_mapping` must not contain undeclared outputs;
- each output mapping entry must contain string `from_node` and `column` values;
- inline nested config sub-workflows are validated recursively.

#### Nested Sub-Workflows

A node with `"type": "sub_workflow"` is treated as a nested sub-workflow rather than a regular tool. Two forms are supported:

- **Inline config:** `"config": {...}` — a nested config dict, recursively interpreted via `SubWorkflow.from_config()`.
- **Class-based reference:** `"sub_workflow_class"` + `"sub_workflow_module"` (and optionally `"sub_workflow_package"` / `"sub_workflow_package_version"`) — imports and instantiates an existing Python `SubWorkflow` subclass.

#### Serialization

When `Workflow.export()` encounters a config-driven sub-workflow, it serializes the config dict directly:

```json
{
  "name": "spot_detection_1",
  "type": "sub_workflow",
  "sub_workflow_type": "config",
  "config": { ... },
  "constants": { ... }
}
```

`Workflow.load()` checks `"sub_workflow_type"`: when `"config"`, it calls `SubWorkflow.from_config(node_data["config"])` to reconstruct the sub-workflow.

#### Equivalence

A config-driven sub-workflow is functionally equivalent to a class-based sub-workflow that performs the same wiring. It produces the same `SubWorkflowNode` type, participates in the same flattening/caching/scoping mechanisms, and is indistinguishable to the execution engine.

---

## 15. Future Work

The following items are acknowledged design concerns that will be addressed in future iterations:

### 15.1 Row-Level Error Policy

Currently, when a single row fails in `process_row`, the entire node execution fails and no partial results are saved. For large datasets (e.g., 10,000 images where one is corrupted), this discards all successful results.

**Planned:** An `on_error` policy per node:
- `on_error="fail"` (default): Current behavior — any row failure aborts the node.
- `on_error="skip"`: Failed rows are excluded from the output DataFrame. A row-level error log is saved alongside the results.
- Partial results saved to cache with a metadata flag marking the node as incomplete, enabling incremental re-execution.

### 15.2 Content-Based Cache Hashing

The signature hash includes file *paths*, not file *contents*. If an input file is modified without changing its path, the cache reports a false hit.

**Planned:** An opt-in `content_hash=True` mode for source nodes that hashes file metadata (size + mtime) or file contents. This is expensive for large files but critical for reproducibility in scientific workflows. When enabled, the source node's signature hash additionally includes the content hash of each file it references.

---

## Appendix A: Wetlands API

Wetlands is a lightweight Python library for managing Conda environments. It creates environments on demand, installs dependencies, and runs Python code inside them as isolated subprocess workers. Each environment is fully isolated, enabling tools with conflicting dependencies to coexist in the same workflow.

### A.1 Environment Manager

```python
from wetlands.environment_manager import EnvironmentManager

manager = EnvironmentManager(
    wetlands_instance_path="wetlands/",
    conda_path="path/to/pixi/",
    main_conda_environment_path=None,
)
```

### A.2 Create an Environment

```python
env = manager.create("cellpose_env", {"conda": ["cellpose==3.1.0"]})
```

- If an environment with this name already exists, Wetlands reuses it.
- `create_from_config()` accepts `requirements.txt`, `environment.yml`, `pyproject.toml`, or `pixi.toml`.

### A.3 Launch Workers

```python
# Single worker (default)
env.launch()

# Multiple workers sharing the same conda environment on disk
env.launch(max_workers=4)

# With per-worker environment variables (e.g., GPU assignment)
env.launch(
    max_workers=4,
    worker_env=lambda i: {"CUDA_VISIBLE_DEVICES": str(i)},
)
```

When `max_workers > 1`, tasks are dispatched to idle workers automatically. When all workers are busy, tasks queue internally and are dispatched as workers become available.

### A.4 Task-Based Execution

```python
# Non-blocking: returns a Task[T]
task = env.submit("module.py", "function_name", args=(arg1, arg2))
task.wait_for()
result = task.result

# Blocking (convenience)
result = env.execute("module.py", "function_name", (arg1, arg2))

# Batch parallel execution — yields results in order
results = list(env.map("module.py", "process", items))

# Batch with per-item Task control
tasks = env.map_tasks("module.py", "process", items)
for task in tasks:
    task.listen(my_callback)
for task in tasks:
    task.wait_for()
```

**Task lifecycle:** `PENDING` → `RUNNING` → `COMPLETED` | `FAILED` | `CANCELED`

**Task API:**
- `task.status` — current `TaskStatus` (has `.is_finished()` for terminal state check)
- `task.result` — return value (only when `COMPLETED`, raises `InvalidStateError` otherwise)
- `task.error` — error message string (only when `FAILED`)
- `task.exception` — `ExecutionException` wrapping error + traceback (only when `FAILED`)
- `task.traceback` — traceback lines (only when `FAILED`)
- `task.progress` — float in [0, 1] or None (computed from `current / maximum`)
- `task.message` — last progress message from `update()`
- `task.current` — current progress counter from `update()`
- `task.maximum` — maximum progress counter from `update()`
- `task.outputs` — dict of named intermediate outputs from `set_output()`
- `task.listen(callback)` — register event listener
- `task.wait_for(timeout=)` — block until terminal state
- `task.cancel()` — request cooperative cancellation
- `task.future` — `concurrent.futures.Future[T]` for interop

### A.5 Progress Reporting and Cancellation (Worker Side)

Remote functions can declare an optional `task` parameter to receive a `RemoteTaskHandle`:

```python
# runs inside the isolated environment
def my_function(data, *, task=None):
    for i, item in enumerate(data):
        if task and task.cancel_requested:
            task.cancel()
            return None
        if task:
            task.update(f"Processing {i+1}/{len(data)}",
                        current=i, maximum=len(data))
        process(item)
    return result
```

Functions without a `task` parameter work exactly as before.

### A.6 Cleanup

```python
env.exit()  # Shuts down all workers and releases resources
```

---

## Changelog

- GUI Validation and Planning API: `Workflow.from_dict` / `to_dict`, `Workflow.validate`, `Workflow.plan`, `Workflow.capture_errors`, `Workflow.topological_order`, `Workflow.downstream_of`, `ValidationError` dataclass, `NodePlan` dataclass, and helpers `validate_parameters` / `check_type_compat` / `serialize_image_spec`. Additive; `Workflow.load` / `export` / `compute` are unchanged. Note: `Workflow.validate()` runs Pydantic validation on supplied constants (`parameter_invalid`) that was previously deferred to execution — callers relying on engine coercion may need explicit defaults or broader `Inputs` types.
- Wire-format schema serializers: `bioimageflow.serialize_input_schema(tool_class)` and `serialize_output_schema(tool_class)` return JSON-safe per-field schemas (including `choices` from `Literal` / `Enum`, three-state `connectable`, `image_spec`) for tool classes without requiring instantiation. `Passthrough` outputs serialize to `{"_passthrough": True}`. `SchemaSerializationError` is the accompanying exception. Recommended for any GUI or external consumer that needs tool metadata over the wire; `get_inputs_schema(tool)` is still available for Python-object introspection. Additive; no existing API changed.
- Platform-boundary refactor (GUI integration surface):
  - `Workflow.from_dict` gains orthogonal `validate_only` and `partial` flags. The legacy `collect_errors=` kwarg is **removed** (passing it raises `TypeError`); `validate_only=True, partial=True` is the equivalent.
  - The `Workflow.collect_errors()` context manager is **renamed** to `Workflow.capture_errors()`; the underlying `_error_capture` ContextVar is consistent. No alias.
  - `Workflow` exposes `errors`, `failed_nodes`, `is_partial` build-time properties and `invalidate(node_ids, *, cascade=True)` for cache cleanup. `invalidate` is **not** safe vs concurrent `compute()`.
  - `Workflow.plan()` adds per-node `NodePlanStatus` (`CACHED` / `OUT_OF_DATE` / `UNEXECUTED` / `SKIPPED`) on the `NodePlan` dataclass; `cached` / `skipped` are read-only convenience accessors derived from `status`. `plan()` raises `CycleInWorkflowError` (a `ValueError` subclass) on cyclic graphs instead of degrading to all-skipped.
  - `ValidationError` adds an `edge_id: str | None` field, copied from the optional `id` key on wire-format edges. `validate()`'s deduplication includes `edge_id`, so two errors that differ only by `edge_id` are reported as distinct.
  - `serialize_constant` / `deserialize_constant` are public exports of `bioimageflow.validation`. `deserialize_constant` requires the typed envelope; bare-string input is no longer accepted.
  - New `bioimageflow.ToolRegistry` and `bioimageflow.ToolMetadata` for GUIs to enumerate tools without rebuilding loader plumbing. `install_package` (network) and `register_package` (in-process) are split so hot validation paths never touch the network.
  - New `bioimageflow.WorkflowSession` — a dict-backed incremental editing model with cached `to_workflow()`, in-place updates for `set_constant` / `set_enabled` (no tool re-resolution), and structural-edit invalidation. Aimed at GUI clients that mutate graphs at keystroke rate.
