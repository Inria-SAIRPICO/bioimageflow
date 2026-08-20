# BioImageFlow

A Python library for orchestrating bioimage analysis workflows as reproducible DAG pipelines.

BioImageFlow lets you declare image-processing tools, wire them into directed acyclic graphs, and execute them with automatic caching, type checking, and provenance tracking.

## Key Features

- **DAG workflow engine** — build pipelines by connecting tools, not writing glue code
- **Two-package architecture** — a minimal worker-safe core (`bioimageflow-core`) and an orchestrator (`bioimageflow`) for the main process
- **Typed image I/O** — annotate inputs/outputs with semantic type, layout, and dtype constraints; reusable groups such as `SCALAR_IMAGE_SEMANTICS` cover common scalar image consumers
- **Automatic caching** — result-key/current-record caching skips redundant computation
- **Shared memory** — zero-copy array transfer between tools via `SharedArray`
- **Companion merge strategies** — inner join, cross join, concat, and collect tools from `bioimageflow-common-tools`
- **Output templating** — declarative output path patterns with `{input.stem}`, `{row_index}`, etc.
- **Environment isolation** — each tool declares its own `EnvironmentSpec` so dependencies never conflict

## Requirements

- Python >= 3.10

`bioimageflow-core` supports Python >= 3.9 for isolated Wetlands worker environments that require Python 3.9 binary dependencies, while the user-facing orchestrator and first-party tool packages target Python >= 3.10.

## Installation

```bash
pip install bioimageflow
```

Install the optional Parsl runtime for distributed processing:

```bash
pip install "bioimageflow[parsl]"
```

Install Parsl together with the PSI/J cluster orchestrator launcher:

```bash
pip install "bioimageflow[parsl,psij]"
```

For development:

```bash
git clone git@github.com:Inria-SAIRPICO/bioimageflow.git
cd bioimageflow
uv sync
```

## Quick Start

```python
from pathlib import Path
from typing import Annotated

from bioimageflow_core import (
    ProcessingTool, RowConsumption, GENERAL_ENV, ImageSpec, Arguments, Template,
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
    row_consumption = RowConsumption.MAPPED
    display_name = "Threshold"
    environment = GENERAL_ENV

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


# 3. Define and run a reusable workflow
from bioimageflow import configure_wetlands

configure_wetlands(root="./wetlands")

WORKFLOW_DIRECTORY = Path(__file__).resolve().parent

def build_workflow(
    *,
    storage_path: str | Path = WORKFLOW_DIRECTORY / "results",
) -> Workflow:
    workflow = Workflow(
        name="threshold_images",
        display_name="Threshold Images",
        storage_path=storage_path,
    )
    with workflow:
        folder = workflow.input("folder", str, id="input-folder")
        cutoff = workflow.input(
            "cutoff", float, default=100.0, id="input-cutoff"
        )
        raw = FileLoader()(folder=folder, name="load_files")
        masks = Threshold()(
            image=raw["path"], cutoff=cutoff, name="threshold"
        )
        workflow.output("mask", masks["mask"], id="output-mask")
    return workflow

workflow = build_workflow()
result = workflow.compute(inputs={"folder": "/data/images"})
print(result)  # DataFrame with a 'mask' column of output paths
```

Saved definitions remain portable because runtime storage is not serialized.
The application loading a workflow chooses that storage explicitly; a simple project convention is to keep it beside the definition:

```python
workflow_directory = Path("workspace/workflows/threshold-images").resolve()

workflow = Workflow.load(
    workflow_directory / "workflow.json",
    storage_path=workflow_directory / "results",
)
```

Persistent archive import keeps the extraction destination and runtime results distinct:

```python
workflow = Workflow.import_archive(
    "threshold-images.zip",
    workflow_directory,
    storage_path=workflow_directory / "results",
)
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

Runs in isolated environments. Implements `process_row()` (one row at a time) or `process_batch()` (all rows at once), and explicitly declares whether rows are mapped independently or consumed collectively. Inputs and outputs are declared as `IOModel` inner classes.

### DataFrameTool

Runs in the main process. Transforms entire DataFrames. Useful for loading data, filtering rows, reshaping tables, or any operation that needs pandas.

## Documentation

Published documentation is available at <https://bioimageflow.readthedocs.io/latest/>.
The documentation source is available in `docs/`:

```bash
uv run python docs/generate_tool_package_docs.py
uv run sphinx-autobuild docs/source docs/_build/html
```

Open the live preview at <http://127.0.0.1:8000/>.

For a one-shot Sphinx build through the docs Makefile:

```bash
cd docs
make html
open build/html/index.html
```

## Exporting Results

Every reusable node record stores its complete DataFrame and declared assets.
To create a self-contained copy of the latest human-facing results after a workflow has run:

```bash
bioimageflow export-outputs ./results
```

The same operation is available without reconstructing the workflow:

```python
from bioimageflow import export_outputs

paths = export_outputs("./results", mode="copy", scope="latest")
```

Each node directory includes the declared assets, canonical `dataframe.parquet`, readable `dataframe.csv` and `dataframe.json` exports, and a `provenance.json` explanation containing the tool/version, parameters, selected upstream records, cache identities, and run metadata.
Use `--scope runs --run-id <run-id>` to export a particular run, or `--scope both` to export both the latest node view and a run view.

To install a self-contained output tree outside workflow storage, pass an explicit destination:

```bash
bioimageflow export-outputs ./results --destination ./shared-results
```

The destination contains `latest/` and/or `runs/<run-id>/` according to the selected scope.
It is staged as a complete sibling tree and must not already exist unless `--replace` is passed.
The Python API provides the equivalent `destination=` and `replace=` keyword arguments.
The `replace` option is invalid when no explicit destination is supplied.

## Development

The full test workflow is documented in `docs/source/reference/testing.md`.

```bash
# Run quality checks
uv run ruff check .
uv run pyright
uv run python scripts/check_file_sizes.py
uv run python scripts/check_import_boundaries.py

# Print focused tests for the current edit
git diff --name-only | uv run python scripts/affected_tests.py --stdin

# Run the independent fast suites used by CI
uv run pytest tests/unit -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"
uv run pytest tests/integration -m "not slow and not acceptance and not packaging and not package_tools and not complete and not wetlands and not public_data and not external_binary and not sairpico_binary and not model_runtime and not parsl"

# Run the real Parsl tiers
uv run pytest tests -m "parsl and not slow"
uv run pytest tests -m "parsl and slow"

# Run regular tests before broad finalization
uv run pytest

# Run deterministic acceptance tests
uv run pytest -m "acceptance and not complete"

# Run deterministic package-tool tests
uv run pytest -m "package_tools and not complete"

# Run only unit tests
uv run pytest tests/unit/

# Run only integration tests
uv run pytest tests/integration/

# Verify package build artifacts
uv run pytest tests/unit/test_package_artifacts.py
uv build --all-packages --no-sources --out-dir dist/packages
BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages uv run pytest tests/unit/test_package_artifacts.py

# Build documentation with warnings treated as failures
uv run python docs/generate_tool_package_docs.py
uv run sphinx-build -W --keep-going docs/source docs/_build/html

# Run opt-in complete tests
uv run pytest -m complete --run-complete -rsx
# -rs reports skipped tests and reasons
# -x stops at first failure

# Run the real Wetlands smoke tier
uv run pytest -m wetlands tests/integration/test_wetlands_smoke.py
```

Package versions and releases are independent.
Check local versions against PyPI with:

```bash
uv run python scripts/package_status.py
```

Package-specific releases use protected annotated tags such as `bioimageflow-core-v0.1.7` and an approval-gated coordinated GitHub Actions publication workflow.
`scripts/release_set.py tag --dry-run` discovers and validates the pending release set, while `tag --push REMOTE` creates and atomically pushes every required annotated tag.
See the [release operator guide](docs/source/reference/releasing.md) for setup and release steps.

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
