# BioImageFlow

A Python library for orchestrating bioimage analysis workflows as reproducible DAG pipelines.

BioImageFlow lets you declare image-processing tools, wire them into directed acyclic graphs, and execute them with automatic caching, type checking, and provenance tracking.

## Key Features

- **DAG workflow engine** — build pipelines by connecting tools, not writing glue code
- **Two-package architecture** — a minimal worker-safe core (`bioimageflow-core`) and an orchestrator (`bioimageflow`) for the main process
- **Typed image I/O** — annotate inputs/outputs with semantic type, layout, and dtype constraints; reusable groups such as `SCALAR_IMAGE_SEMANTICS` cover common scalar image consumers
- **Automatic caching** — v1 result-key/current-record caching skips redundant computation
- **Shared memory** — zero-copy array transfer between tools via `SharedArray`
- **Companion merge strategies** — inner join, cross join, concat, and collect tools from `bioimageflow-common-tools`
- **Output templating** — declarative output path patterns with `{input.stem}`, `{row_index}`, etc.
- **Environment isolation** — each tool declares its own `EnvironmentSpec` so dependencies never conflict

## Requirements

- Python >= 3.10

## Installation

```bash
pip install bioimageflow
```

For development:

```bash
git clone https://gitlab.inria.fr/sairpico/bioimageflow.git
cd bioimageflow
uv sync
```

## Quick Start

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import (
    ProcessingTool, EnvironmentSpec, ImageSpec, Arguments, Template,
)
from bioimageflow import Workflow, DataFrameTool

# 1. Define a source tool (DataFrameTool produces a DataFrame)
class FileLoader(DataFrameTool):
    display_name = "File Loader"

    class Inputs:
        folder: str

    def transform(self, df, arguments):
        import pandas as pd
        from pathlib import Path

        files = sorted(Path(arguments.folder).glob("*.tif"))
        return pd.DataFrame({"path": [str(f) for f in files]})


# 2. Define a processing tool (runs in an isolated environment)
class Threshold(ProcessingTool):
    display_name = "Threshold"
    environment = EnvironmentSpec(
        name="imageio",
        dependencies={"python": "3.10", "pip": ["imageio", "numpy"]},
    )

    class Inputs:
        image: Annotated[Path, ImageSpec()]
        cutoff: float = 128.0

    class Outputs:
        mask: Annotated[Path, ImageSpec(semantics={"binary"})] = Template(
            "{image.stem}_mask.tif"
        )

    def process_row(self, arguments: Arguments) -> "Threshold.Outputs":
        import imageio.v3 as iio
        import numpy as np

        img = iio.imread(arguments.image)
        mask = (img > arguments.cutoff).astype(np.uint8) * 255
        iio.imwrite(arguments.mask, mask)
        return self.Outputs(mask=arguments.mask)


# 3. Build and run the workflow
from bioimageflow import configure_wetlands

threshold = Threshold()
loader = FileLoader()

configure_wetlands(wetlands_instance_path="./wetlands")

with Workflow(storage_path="./bif_data", engine="wetlands") as wf:
    raw = loader(folder="/data/images")
    masks = threshold(image=raw["path"], cutoff=100.0, name="threshold")
    result = wf.compute(masks)

print(result)  # DataFrame with a 'mask' column of output paths
```

## Architecture

```
bioimageflow-core          bioimageflow
(worker-safe + numpy)      (pandas + pydantic, main process)
┌─────────────────────┐   ┌──────────────────────────┐
│  Semantic, Layout    │   │  Workflow                 │
│  ImageSpec, groups   │   │  Node, ColumnRef          │
│  ProcessingTool      │   │  SequentialEngine         │
│  IOModel, Arguments  │   │  DataFrameTool            │
│  EnvironmentSpec     │   │  Tool registry            │
│  SharedArray, I/O    │   │  Cache, Storage, Template │
└─────────────────────┘   └──────────────────────────┘
```

**`bioimageflow-core`** is installed everywhere — main process and worker environments. It contains the type system, tool base classes, and shared-memory utilities, and declares NumPy because shared-memory array views use it at runtime.

**`bioimageflow`** is the orchestrator. It builds the DAG, resolves bindings, executes tools in topological order, and manages caching. It depends on pandas and pydantic.

## Tool Types

### ProcessingTool

Runs in isolated environments. Implements `process_row()` (one row at a time) or `process_batch()` (all rows at once). Inputs and outputs are declared as `IOModel` inner classes.

### DataFrameTool

Runs in the main process. Transforms entire DataFrames. Useful for loading data, filtering rows, reshaping tables, or any operation that needs pandas.

## Documentation

Full documentation is available at `docs/`:

```bash
cd docs
make html
open build/html/index.html
```

## Development

The full test workflow is documented in `docs/source/reference/testing.md`.

```bash
# Run quality checks
uv run ruff check .
uv run pyright

# Run regular tests
uv run pytest

# Run the CI default deterministic test tier
uv run pytest -m "not slow"

# Run only unit tests
uv run pytest tests/unit/

# Run only integration tests
uv run pytest tests/integration/

# Verify package build artifacts
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --out-dir dist/packages

# Build documentation with warnings treated as failures
uv run sphinx-build -W --keep-going docs/source docs/_build/html

# Run opt-in complete tests
uv run pytest -m complete --run-complete -rsx
# -rs reports skipped tests and reasons
# -x stops at first failure

# Run the real Wetlands smoke tier
uv run pytest -m wetlands tests/integration/test_wetlands_smoke.py
```

## FAQ

### Why tools should be instantiated before being executed? `download = DownloadImages()(urls=CIL_URLS)` instead of `download = DownloadImages(urls=CIL_URLS)`?

1. Tool reuse is a first-class use case, not a rare edge case. The spec shows it prominently, and the test suite exercises it extensively (two 
segmenter nodes with different diameters). It enables branching and parameter sweeps without duplicating class definitions.                    
2. The two-call pattern is explicit and clear: tool = MyTool(); node = tool(...). It reads as "create a tool, then use it." This is similar to 
many Python APIs (e.g., logger = logging.getLogger(name); logger.info(...)).                                                                   
3. Cannot have both patterns safely without causing confusion. Supporting both would require magic behavior based on whether kwargs are        
present, which is error-prone (e.g., what if a tool has no required inputs? tool() vs tool(constant=1) would return different types).          
4. Implementation complexity: Using __new__ to return a Node is a non-standard pattern that would confuse readers and tools (type checkers,    
IDEs). It would also make it harder to access tool metadata on the node (you'd need node.tool anyway, so why hide the tool?).                  
                        

## License

BSD 4-Clause License.
