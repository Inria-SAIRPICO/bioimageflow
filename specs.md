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

### 2.3 Type Factories

```python
def ImagePath(
    semantics=None, layouts=None, dtypes=None, formats=None
) -> Any:
    """Returns Annotated[Path, ImageSpec(...)]. Used for file-based image data."""

def ImageShared(
    semantics=None, layouts=None, dtypes=None
) -> Any:
    """Returns Annotated[SharedArray, ImageSpec(...)]. Formats is implicitly {'memory'}."""
```

All parameters accept a single value, a set, or `None` (wildcard).

**Usage examples:**
```python
from bioimageflow_core import ImagePath, ImageShared, Semantic, Layout

# File-based MRI input
MRI_File = ImagePath(semantics=Semantic.INTENSITY, layouts=Layout.VOLUMETRIC, formats={".nii.gz"})

# Shared memory video stream
Video_Stream = ImageShared(semantics=Semantic.INTENSITY, layouts=Layout.PLANAR_TIME_CHANNEL, dtypes="uint8")
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

### 2.5 Interface Type Constraints

`Inputs` and `Outputs` models must use only standard-library types and `bioimageflow-core` types (`ImagePath`, `ImageShared`). Third-party types (NumPy arrays, PIL images, etc.) are **not** allowed in the interface — they cannot cross the serialization boundary. `Outputs` is required on `ProcessingTool` (defines the serialization contract and output templates). On `DataFrameTool`, `Outputs` is optional — when declared, it enables construction-time validation of downstream column references (see [Section 3.4](#34-dataframetool)).

**Runtime type resolution:** `ImagePath` and `ImageShared` are distinct for graph-level compatibility checking (`check_compatibility`), but the orchestrator's Pydantic model builder resolves both to `Union[Path, str, SharedArray]` at validation time. This is necessary because caching may convert a `SharedArray` output to a file `Path` (see [Section 8.2](#82-lifecycle)), and the reverse can happen when shared memory is enabled. Tools should use `load_image()` which handles both transparently.

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
from bioimageflow_core import ProcessingTool, GENERAL_ENV, IOModel, Arguments, ImagePath, Semantic

class ExtractChannel(ProcessingTool):
    name = "extract_channel"
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: ImagePath(semantics=Semantic.INTENSITY)
        channel: int = 0

    class Outputs(IOModel):
        output_image: ImagePath(semantics=Semantic.INTENSITY) = "{input_image.stem}_ch{channel}{ext}"

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

    def process_row(self, arguments: Arguments) -> "Outputs | list[Outputs]":
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

    def process_batch(self, arguments_list: "list[Arguments]") -> "list[list[Outputs]] | list[Outputs]":
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

**Direct tool definition:**
```python
from bioimageflow_core import ProcessingTool, IOModel, ImagePath, Semantic, Arguments, Category

class MySegmenter(ProcessingTool):
    name = "my_segmenter"
    documentation = "Segments cells."
    category = Category.SEGMENTATION
    tags = ["segmentation"]
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: ImagePath(semantics=Semantic.INTENSITY)
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: ImagePath(semantics=Semantic.LABEL) = "{input_image.stem}_mask_{row_index}.png"
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
        input_image: ImagePath(semantics=Semantic.INTENSITY)
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: ImagePath(semantics=Semantic.LABEL) = "{input_image.stem}_mask_{row_index}.png"
        cell_count: int

    def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
        import cellpose.models
        ...

class CellposeTrain(CellposeBase):
    name = "cellpose_train"
    documentation = "Trains a custom Cellpose model."
    tags = ["cellpose", "training"]

    class Inputs(IOModel):
        training_images: ImagePath(semantics=Semantic.INTENSITY)
        training_masks: ImagePath(semantics=Semantic.LABEL)
        epochs: int = 100

    class Outputs(IOModel):
        model_path: Path = "{node_name}_model_{timestamp}"

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
- **`class Outputs(IOModel)`**: Explicit output schema. The engine validates downstream ColumnRefs against this declaration at construction time. Supports full `IOModel` annotations including `ImagePath`/`ImageShared` type metadata for downstream type compatibility checks.

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
| `Outputs`       | `IOModel subclass \| Passthrough subclass \| —` | Optional output schema for construction-time validation (see above) |

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

- **`Inputs`**: Declared on both `ProcessingTool` and `DataFrameTool`. Fields typed as `ImagePath` or `ImageShared` represent data dependencies; scalar fields represent parameters. Default values are supported.
- **`Outputs`**: Required on `ProcessingTool`, optional on `DataFrameTool`. On `ProcessingTool`, fields with string defaults are **output templates** resolved by the engine before execution (see [Section 7.1](#71-output-templating-engine)); fields without string defaults (e.g., `cell_count: int`) are computed values returned by the tool. On `DataFrameTool`, `Outputs` enables construction-time validation of downstream column references. `DataFrameTool` may also declare `class Outputs(Passthrough): pass` to indicate that all input columns are preserved.

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

`Inputs` fields can carry optional `GUIMeta` annotations that provide hints to GUI frontends (e.g., node editors, property panels). `GUIMeta` is a frozen dataclass attached via `typing.Annotated`, following the same pattern as `ImageSpec`.

```python
@dataclass(frozen=True)
class GUIMeta:
    """
    GUI hints for an Inputs field.
    Attached via Annotated — invisible to runtime logic, read by frontends.
    """
    connectable: bool = True   # Can this field be wired to an upstream column?
    min: float | int | None = None   # Minimum value (numeric fields)
    max: float | int | None = None   # Maximum value (numeric fields)
    step: float | int | None = None  # Step increment (numeric fields)
    group: str | None = None   # Logical group for tab/section display (e.g. "general", "advanced", "gpu")
```

**Defaults:** Fields without a `GUIMeta` annotation default to `connectable: True` with no numeric constraints and no group. A GUI frontend inspects the `Annotated` metadata for each field; if no `GUIMeta` is found, it assumes the field is connectable with no min/max/step and belongs to the default (unnamed) group.

**Usage:**

```python
from typing import Annotated
from bioimageflow_core import ProcessingTool, IOModel, ImagePath, Semantic, Arguments, GUIMeta

class CellposeSegmenter(ProcessingTool):
    name = "cellpose_segmenter"
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: ImagePath(semantics=Semantic.INTENSITY)
        diameter: Annotated[float, GUIMeta(min=0.0, max=500.0, step=0.5, group="general")] = 30.0
        model_type: Annotated[str, GUIMeta(connectable=False, group="general")] = "cyto3"
        flow_threshold: Annotated[float, GUIMeta(min=0.0, max=1.0, step=0.05, group="advanced")] = 0.4
        use_gpu: Annotated[bool, GUIMeta(connectable=False, group="gpu")] = True

    class Outputs(IOModel):
        mask: ImagePath(semantics=Semantic.LABEL) = "{input_image.stem}_mask_{row_index}.png"
        cell_count: int

    def process_row(self, arguments: Arguments) -> Outputs | list[Outputs]:
        ...
```

In this example:
- `input_image` has no `GUIMeta` → defaults to `connectable: True`, no numeric constraints, default group.
- `diameter` is connectable (default) with a slider range of 0–500, step 0.5, in the **general** tab.
- `model_type` is **not connectable**, in the **general** tab — rendered as a text field or dropdown, never as an input port.
- `flow_threshold` is in the **advanced** tab — hidden from the main view, accessible via an "Advanced" tab.
- `use_gpu` is in the **gpu** tab — grouped with other GPU-related settings.

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

**Compatibility with ImagePath/ImageShared:** Since `ImagePath(...)` already returns `Annotated[Path, ImageSpec(...)]`, a field can carry both `ImageSpec` and `GUIMeta` by nesting: `Annotated[ImagePath(...), GUIMeta(connectable=True)]`. In practice, image fields are almost always connectable, so `GUIMeta` is rarely needed on them.

**Runtime behavior:** `GUIMeta` is purely declarative metadata — it has no effect on validation, execution, caching, or hashing. The orchestrator and worker environments ignore it entirely. It exists solely for GUI frontends to render appropriate widgets and port visibility.

### 3.6 Arguments and Column References

*Module: `bioimageflow_core.arguments` (Arguments), `bioimageflow.node` (ColumnRef)*

#### The `Arguments` Object

When the engine dispatches work to a tool, it constructs an `Arguments` namespace. For `ProcessingTool`, one `Arguments` per row containing all resolved input values and output template paths. For `DataFrameTool`, a single `Arguments` containing the tool's constant parameters.

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

Imports from `bioimageflow-core` (e.g., `ImagePath`, `Arguments`, `IOModel`) can be at module level since the package is always available and has zero external dependencies.

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
- **`ColumnRef`** is created by subscripting a Node: `node["col"]`. It records the upstream node and column name. The engine validates column existence at construction time for upstream nodes with known output schemas (i.e., nodes whose tool declares `Outputs`). For DataFrameTool nodes without `Outputs` — whose schema is dynamic — validation is deferred to execution time.
- The graph must remain a DAG. Cycles are detected synchronously inside `__call__()` when the edge is created, providing instant feedback in scripts and notebooks.
- **Source nodes** are simply nodes with no upstream data dependencies — they are not a separate tool type or code path. Both tool types can act as source nodes:
  - A **DataFrameTool** with no positional arguments receives an empty `dfs` list in `merge_dataframes` and produces the initial DataFrame (e.g., by listing files in a directory).
  - A **ProcessingTool** with no `ColumnRef` or `Node` arguments (only constants or defaults) is executed through the same code path as any other ProcessingTool. With no column bindings, the engine uses a single-row index (`["0"]`), builds arguments from constants and defaults only, and dispatches to `process_row`/`process_batch` as usual. This is useful when listing or loading files requires specialized libraries (e.g., reading HDF5 headers, DICOM metadata, OME-TIFF pyramids) that should not pollute the main process.

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
| `storage_path`  | `str \| Path` | `"./bif_data"`  | Root directory for output files and cache         |
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
- Parameter bindings (constants, column references with upstream node names).
- Node enabled/disabled state (see [Section 4.6](#46-enabling-and-disabling-nodes)).
- Graph edges (upstream-downstream relationships).
- Workflow-level configuration (storage path, cache policy, engine choice).

Tool code is **not** serialized — the same tool packages (at the referenced versions) must be available in the tool store to re-execute a loaded workflow.

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

| Field         | Type   | Description                                            |
|--------------|--------|--------------------------------------------------------|
| `node_name`   | `str`  | Name of the node being executed                        |
| `status`      | `str`  | One of: `"started"`, `"row_complete"`, `"completed"`, `"cached"`, `"failed"` |
| `row`         | `int`  | Current row index (for `row_complete`)                 |
| `total_rows`  | `int`  | Total number of rows for this node                     |
| `timestamp`   | `float`| Unix timestamp                                         |

The callback is invoked from the main process. For the parallel engine, events from concurrent nodes may interleave. The callback must be thread-safe if using the Parsl engine.

### 4.5 Input Binding Logic (Graph Construction)

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
   2. **Resolve Arguments:** Resolve `Inputs` parameters into a single `Arguments` object (all constants, validated via Pydantic).
   3. **Cache Check:** Compute the [signature hash](#61-signature-hash). If a cache hit exists, load cached results and skip to step 6.
   4. **Merge:** Call `tool.merge_dataframes(dfs, arguments)`. Default: inner join on index.
   5. **Transform:** Call `tool.transform(df, arguments)`. Returns a (potentially different) DataFrame. Default: identity (passthrough).
   6. **Caching:** Save the result DataFrame and metadata to the [storage structure](#72-directory-structure).

#### ProcessingTool Execution Path

   1. **Index Alignment:** Collect all upstream nodes referenced via column bindings. Compute the aligned index — the finest-grained index that is compatible with all upstream indices (see [Section 5.3](#53-dataframe-semantics)). If upstream indices are incompatible (no common lineage), raise `IndexAlignmentError`.
   2. **Value Resolution:** For each row in the aligned index, materialize input values from the column bindings. The orchestrator validates resolved values using Pydantic models built from the tool's `IOModel` declarations.
   3. **Output Templating:** Resolve output path templates for every row (see [Section 7.1](#71-output-templating-engine)). The main process must resolve output paths *before* dispatch since the worker has no knowledge of workflow context.
   4. **Cache Check:** Compute the [signature hash](#61-signature-hash). If a cache hit exists, load cached results and skip to step 9.
   5. **Serialization:** Convert resolved values to `list[dict]` (one dict per row, containing all resolved input values and output paths).
   6. **Environment Launch:** If not already running, create/reuse the Wetlands environment. If an environment with the same name already exists but its dependency hash differs, raise `EnvironmentMismatchError`.
   7. **Dispatch:** If `process_batch` was overridden, call it with the full arguments list. Otherwise, call `process_row` for each row individually (potentially in parallel).
   7b. **Output Validation (worker-side):** After `process_row`/`process_batch` returns, the worker performs lightweight `isinstance` checks on each output field against the tool's `Outputs` annotations (e.g., `ImagePath`-typed fields must be `Path` or `str`, `int` fields must be `int`). These checks use only the standard library (no Pydantic) and add negligible overhead. Errors are raised immediately in the worker with clear stack traces pointing to the tool code.
   8. **DataFrame Construction:** Build the output DataFrame from the tool's results. The output contains **only** the columns declared in `Outputs` (no upstream columns are carried forward). The index is preserved from the aligned input index, with explosion for 1-to-N outputs (see Section 5.3).
   9. **Caching:** Save the result DataFrame and metadata to the [storage structure](#72-directory-structure).

#### Orchestrator-Worker Interaction (ProcessingTool Steps 6-9)

The orchestrator drives all calls into the environment using Wetlands' proxy API. The tool's **module path** (`tool.__module__`) tells the orchestrator which Python module to load in the worker. The orchestrator imports this module in the worker via `env.import_module()`, then calls a dispatcher function that instantiates the tool class by name.

```python
# === Orchestrator (main process) ===

# 6. Environment Launch
env = environment_manager.create(tool.environment.name, tool.environment.dependencies)
env.launch()

# The orchestrator loads the module that defines the tool class.
# tool.__module__ is the standard Python attribute (e.g., "my_tools.cellpose_tools").
worker_module = env.import_module(tool.__module__)

# 7. Check if process_batch was overridden
has_batch = type(tool).process_batch is not ProcessingTool.process_batch

# 8. Dispatch: batch or per-row
# The orchestrator passes the tool class name so the worker can instantiate it.
tool_class_name = type(tool).__name__

if has_batch:
    results = worker_module.run_process_batch(tool_class_name, arguments_dicts)
else:
    results = []
    for args_dict in arguments_dicts:
        row_result = worker_module.run_process_row(tool_class_name, args_dict)
        results.append(row_result)

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

def run_process_batch(tool_class_name, arguments_dicts):
    tool = _get_instance(tool_class_name)
    args_list = [Arguments(**d) for d in arguments_dicts]
    results = tool.process_batch(args_list)
    # Auto-wrap list[Outputs] → list[list[Outputs]] for 1-to-1 batch tools
    if results and not isinstance(results[0], list):
        results = [[r] for r in results]
    return [[vars(out) for out in row_outputs] for row_outputs in results]

def run_process_row(tool_class_name, arguments_dict):
    tool = _get_instance(tool_class_name)
    args = Arguments(**arguments_dict)
    result = tool.process_row(args)
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

### 6.3 Limitations

- **Path-based, not content-based:** The hash includes file *paths*, not file *contents*. If an input file is modified without changing its path, the cache will report a false hit. Users can manually invalidate the cache when needed.
- **Transitive dependency changes:** The `env_dependencies_hash` catches version spec changes (e.g., `cellpose==3.0` → `cellpose==4.0`). However, if a dependency releases a bug fix *without* changing the pinned version (e.g., a Conda rebuild of `cellpose==3.0`), the cache will not invalidate. Bump the tool's package version or use `dev_mode` to force re-execution.

### 6.4 Cache Retention Policy

Users configure cache retention per workflow:

- **`max_executions`** (default: `0`): Number of past executions to keep. `0` means result files are deleted when a new execution completes. Higher values (e.g., `3`, `100`) retain that many historical results.
- **`max_age`** (optional): Maximum age for cached results. Results older than this are eligible for deletion. Cleanup runs at workflow execution time, or can be triggered via a dedicated cleanup function.

---

## 7. File Management

### 7.1 Output Templating Engine

BioImageFlow enforces structured file naming to prevent overwrites and maintain order. Output fields in `ProcessingTool.Outputs` with string defaults are treated as path templates, resolved by the engine before dispatch. (DataFrameTool does not use output templating — it returns DataFrames directly.)

**Template detection rule:** Only fields whose type annotation is Path-based (`Path`, `ImagePath(...)`, `Annotated[Path, ...]`) have their string defaults interpreted as templates. Non-path string fields (e.g., `model_name: str = "default"`) are treated as literal default values, never as templates. This eliminates ambiguity without requiring a separate `Template(...)` marker type.

**Available template variables:**

| Variable                   | Description                                            |
|---------------------------|--------------------------------------------------------|
| `{node_name}`             | Name of the current node/step                          |
| `{row_index}`             | Global index of the item in the DataFrame              |
| `{<input_field>.name}`    | Original filename of the named input path field        |
| `{<input_field>.stem}`    | Filename without extension                             |
| `{<input_field>.ext}`     | Last file extension (e.g., `.gz`)                      |
| `{<input_field>.exts}`    | All file extensions (e.g., `.tar.gz`)                  |
| `{column:<column_name>}`  | Value from the named DataFrame column for this row     |
| `{timestamp}`             | Execution timestamp                                    |

`<input_field>` must be the name of an `Inputs` field typed as a path (e.g., `input_image`).

**Default template:** `{node_name}_{row_index}{ext}`

**`{ext}` resolution:** If the tool has exactly one input path field, `{ext}` resolves to its extension. Otherwise (zero or multiple input paths), `{ext}` resolves to an empty string — the tool author must specify the extension explicitly in the template (e.g., `.tif` or `{input_image.ext}`).

**Example:**
```python
class Outputs(IOModel):
    mask: ImagePath(semantics=Semantic.LABEL) = "{input_image.stem}_mask_{row_index}.png"
```
For a row where `input_image` is `/data/cell_01.tif` and `row_index` is `3`, this resolves to `cell_01_mask_3.png`.

**1-to-N output naming:** The resolved template passed in `Arguments` acts as a **base path**. The tool may mutate it to create non-colliding names (e.g., `base_path.with_name(f"{base_path.stem}_tile{i}{base_path.suffix}")`). The engine builds the output DataFrame from the paths returned in `Outputs` objects, not from the pre-resolved templates.

### 7.2 Directory Structure

```text
/workflow_root/
  ├── data/
  │   └── <node_name>/
  │       └── <YYYYMMDD_HHMMSS>_<hash_12chars>/
  │           ├── metadata.json     # Tool version, timestamp, user
  │           ├── parameters.json   # Resolved configuration
  │           ├── dataframe.csv     # Output table
  │           └── assets/           # Output files (ProcessingTool only)
  │               ├── img1_seg.tif
  │               └── img2_seg.tif
  └── provenance_graph.json         # Full DAG dump
```

The hash directory name is prefixed with a creation timestamp (`YYYYMMDD_HHMMSS`) for easy chronological sorting, followed by the first 12 characters of the signature hash (e.g., `20260309_143022_a1b2c3d4e5f6`). Cache lookup matches directories by the trailing hash suffix.

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

- The **simple engine** ignores resource declarations (everything runs sequentially).
- The **parallel engine (Parsl)** maps resource specs to its executor model — e.g., `gpu=1` routes to a GPU executor pool, `max_concurrent=4` limits concurrent task submissions.
- Tools without `resources` have no constraints (unlimited concurrency, CPU-only).

`ResourceSpec` lives in `bioimageflow_core.environment` alongside `EnvironmentSpec`.

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

- **Simple engine:** Executes nodes and rows sequentially. Suitable for debugging and small workflows.
- **Parallel engine (Parsl):** Executes independent workflow branches in parallel and dispatches `process_row` calls concurrently. Parsl handles task scheduling and resource management. The parallel engine uses `ResourceSpec` declarations (see [Section 10](#10-resource-constraints)) to route tasks to appropriate executors.
- **DataFrameTool nodes** always execute sequentially in the main thread (serialized via a lock). This avoids thread-safety requirements on tool authors. Independent DataFrameTool nodes on different branches still benefit from parallel scheduling of their upstream ProcessingTool dependencies.

The choice of engine is transparent to tool authors — the same tool code works with both.

---

## 13. Import Cheat Sheet

```python
# === bioimageflow-core (available in all environments) ===
from bioimageflow_core import (
    # Types
    Semantic, Layout, ImageSpec, SharedArray, ImagePath, ImageShared,
    check_compatibility,
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
)
from bioimageflow.node import Node, ColumnRef
from bioimageflow.engine import DisabledNodeError
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
from bioimageflow_core import IOModel, ImagePath, Semantic, Arguments

class SegmentAndMeasure(SubWorkflow):
    name = "segment_and_measure"

    class Inputs(IOModel):
        image: ImagePath(semantics=Semantic.INTENSITY)
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: ImagePath(semantics=Semantic.LABEL)
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
| `type`       | `str`  | Yes      | One of: `"int"`, `"float"`, `"str"`, `"bool"`, `"Path"` |
| `image_spec` | `dict` | No       | If present, wraps the type with `Annotated[type, ImageSpec(...)]` |
| `default`    | any    | No       | Default value for the field                       |

**`image_spec` dict:** `{"semantics": [...], "layouts": [...], "dtypes": [...], "formats": [...]}`. Values are lists of enum value strings (e.g., `"intensity"`, `"label"`, `"YX"`). All keys are optional; missing keys mean "any" (empty set).

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

Wetlands is a lightweight Python library for managing Conda environments. It can create environments on demand, install dependencies, and execute arbitrary code within them. Each environment remains isolated, enabling tools with conflicting dependencies (e.g., Stardist and Cellpose) to coexist in the same workflow.

### A.1 Environment Manager

```python
from wetlands.environment_manager import EnvironmentManager

environment_manager = EnvironmentManager(
    wetlands_instance_path="wetlands/",        # Logs and debug info (default: "wetlands/")
    conda_path="path/to/pixi/",                # Pixi/Micromamba location (default: inside instance path)
    main_conda_environment_path=None            # Optional: if set, Wetlands checks if dependencies
                                                # are already satisfied in this environment first
)
```

> **Note:** On Windows, spaces are not allowed in `conda_path`.

### A.2 Create an Environment

```python
env = environment_manager.create(
    "cellpose_env",                            # Environment name
    {"conda": ["cellpose==3.1.0"]},            # Dependencies
    use_existing=False                         # If True, reuse any env satisfying the deps
)
```

- If an environment with this name already exists, Wetlands reuses it (**ignoring the provided dependencies**). BioImageFlow therefore compares dependency hashes before reuse and raises `EnvironmentMismatchError` if they differ.
- If `main_conda_environment_path` was provided and the main environment satisfies the dependencies, it is returned directly.
- Wetlands supports PEP 440 version specifiers (e.g., `"numpy>=1.20,<2.0"`).
- `create_from_config()` accepts `requirements.txt`, `environment.yml`, `pyproject.toml`, or `pixi.toml`.
- `load()` loads an existing environment by path.

### A.3 Launch and Execute

```python
# Start the communication server in the isolated environment
env.launch()

# Option 1: Proxy-based execution (accepts file path or dotted module path)
module = env.import_module("my_tools/cellpose_tools.py")
result = module.segment(str(image_path), str(output_path))

# Option 2: Direct execution
result = env.execute("my_tools/cellpose_tools.py", "segment", (str(image_path), str(output_path)))
```

- `env.import_module()` returns a proxy object. Calling methods on it sends the call to the worker, executes the real function there, and returns the result.
- All function arguments and return values must be picklable.

### A.4 Cleanup

```python
env.exit()  # Shuts down the communication server and releases resources
```
