# BioImageFlow

A Python library for orchestrating bioimage analysis workflows as reproducible DAG pipelines.

BioImageFlow lets you declare image-processing tools, wire them into directed acyclic graphs, and execute them with automatic caching, type checking, and provenance tracking.

## Key Features

- **DAG workflow engine** — build pipelines by connecting tools, not writing glue code
- **Two-package architecture** — a zero-dependency core (`bioimageflow-core`) safe for worker processes, and an orchestrator (`bioimageflow`) for the main process
- **Typed image I/O** — annotate inputs/outputs with semantic type, layout, and dtype constraints; the framework checks compatibility at graph-construction time
- **Automatic caching** — signature-hash based caching skips redundant computation
- **Shared memory** — zero-copy array transfer between tools via `SharedArray`
- **Merge strategies** — built-in inner join, cross join, concat, and collect operations
- **Output templating** — declarative output path patterns with `{input.stem}`, `{row_index}`, etc.
- **Environment isolation** — each tool declares its own `EnvironmentSpec` so dependencies never conflict

## Requirements

- Python >= 3.13

## Installation

```bash
pip install bioimageflow
```

For development:

```bash
git clone https://github.com/your-org/bioimageflow.git
cd bioimageflow
uv sync
```

## Quick Start

```python
from bioimageflow_core import ProcessingTool, EnvironmentSpec, ImagePath, Arguments
from bioimageflow import Workflow, DataFrameTool

# 1. Define a source tool (DataFrameTool produces a DataFrame)
class FileLoader(DataFrameTool):
    name = "file_loader"

    class Inputs:
        folder: str

    def transform(self, df, arguments):
        import pandas as pd
        from pathlib import Path

        files = sorted(Path(arguments.folder).glob("*.tif"))
        return pd.DataFrame({"path": [str(f) for f in files]})


# 2. Define a processing tool (runs in an isolated environment)
class Threshold(ProcessingTool):
    name = "threshold"
    environment = EnvironmentSpec(name="base", dependencies={})

    class Inputs:
        image: ImagePath()
        cutoff: float = 128.0

    class Outputs:
        mask: ImagePath(semantics={"binary"}) = "{image.stem}_mask.tif"

    def process_row(self, arguments: Arguments) -> "Threshold.Outputs":
        import numpy as np
        from skimage.io import imread, imsave

        img = imread(arguments.image)
        mask = (img > arguments.cutoff).astype(np.uint8) * 255
        imsave(str(arguments.mask), mask)
        return self.Outputs(mask=arguments.mask)


# 3. Build and run the workflow
threshold = Threshold()
loader = FileLoader()

with Workflow(storage_path="./bif_data") as wf:
    raw = loader(folder="/data/images")
    masks = threshold(image=raw["path"], cutoff=100.0)
    result = wf.compute(masks)

print(result)  # DataFrame with a 'mask' column of output paths
```

## Architecture

```
bioimageflow-core          bioimageflow
(zero deps, worker-safe)   (pandas + pydantic, main process)
┌─────────────────────┐   ┌──────────────────────────┐
│  Semantic, Layout    │   │  Workflow                 │
│  ImageSpec           │   │  Node, ColumnRef          │
│  ProcessingTool      │   │  SequentialEngine         │
│  IOModel, Arguments  │   │  DataFrameTool            │
│  EnvironmentSpec     │   │  Merge strategies         │
│  SharedArray, I/O    │   │  Cache, Storage, Template │
└─────────────────────┘   └──────────────────────────┘
```

**`bioimageflow-core`** is installed everywhere — main process and worker environments. It contains the type system, tool base classes, and shared-memory utilities with zero external dependencies.

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
open _build/html/index.html
```

## Development

```bash
# Run all tests
uv run pytest

# Run only unit tests
uv run pytest tests/unit/

# Run only integration tests
uv run pytest tests/integration/
```

## License

BSD 4-Clause License.
