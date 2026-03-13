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

