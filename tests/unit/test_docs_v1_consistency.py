"""Documentation contracts for the clean v1 public behavior."""

from pathlib import Path
import re


ROOT = Path(__file__).parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_user_docs_do_not_advertise_legacy_timestamp_cache_tree() -> None:
    docs = [
        "docs/source/quickstart.rst",
        "docs/source/tutorials/basic_workflow.rst",
        "docs/source/concepts/execution.rst",
        "docs/source/concepts/caching.rst",
    ]
    legacy_fragments = [
        "bif_data/data",
        "storage_path/data",
        "data/<node_name>/<timestamp>_<hash>",
        "<timestamp>_<hash>",
        "<timestamp>_<hash>/assets",
    ]

    offenders = []
    for path in docs:
        text = _text(path)
        for fragment in legacy_fragments:
            if fragment in text:
                offenders.append(f"{path}: {fragment}")

    assert offenders == []


def test_execution_docs_describe_v1_cache_layout_without_result_file_in_result_key_dir() -> None:
    execution = _text("docs/source/concepts/execution.rst")

    assert "cache/v1" in execution
    assert "records/{record_id}/" in execution
    assert "runs/" in execution
    assert "latest/" in execution
    assert "├── result.json" not in execution


def test_engine_docs_do_not_describe_wetlands_row_workers_as_default() -> None:
    docs = {
        "docs/source/concepts/execution.rst": _text("docs/source/concepts/execution.rst"),
        "docs/source/tutorials/parallelism.rst": _text("docs/source/tutorials/parallelism.rst"),
    }
    stale_fragments = [
        "The default engine runs **independent nodes concurrently** and dispatches",
        "BioImageFlow's default engine runs work in parallel out of the box",
        "Rows of a single ProcessingTool** run **in parallel** across the",
    ]

    offenders = []
    for path, text in docs.items():
        for fragment in stale_fragments:
            if fragment in text:
                offenders.append(f"{path}: {fragment}")

    assert offenders == []


def test_installation_docs_name_supported_python_matrix() -> None:
    installation = _text("docs/source/installation.rst")

    assert "Python >= 3.10" in installation
    assert "bioimageflow-core`` package supports Python >= 3.9" in installation
    assert "Python 3.10, 3.11, and 3.12" in installation


def test_specs_document_transitional_result_key_and_record_identity_limits() -> None:
    specs = _text("docs/source/specs.md")
    storage_reference = _text("docs/source/reference/output_cache_storage.md")

    for text in [specs, storage_reference]:
        assert "result-key composition is still transitional" in text
        assert "diagnostic logical signature" in text

    assert "publication currently hashes the staged Parquet file bytes" in storage_reference
    assert "Parquet writer metadata is part of the implemented record ID today" in storage_reference


def test_specs_do_not_claim_shared_memory_cleanup_cli_exists() -> None:
    specs = _text("docs/source/specs.md")

    assert "No `bioimageflow clean-shm` CLI is currently provided" in specs
    assert "The engine registers an `atexit` handler" not in specs


def test_merge_tools_are_documented_as_common_tools_exports() -> None:
    architecture = _text("docs/source/concepts/architecture.rst")
    merge_tutorial = _text("docs/source/tutorials/merge_strategies.rst")

    assert ":class:`~bioimageflow.InnerJoin`" not in merge_tutorial
    assert "from bioimageflow import InnerJoin" not in merge_tutorial
    assert "from bioimageflow import CrossJoin" not in merge_tutorial
    assert "from bioimageflow import JoinOnColumn" not in merge_tutorial
    assert "from bioimageflow import Concat" not in merge_tutorial
    assert "from bioimageflow import Collect" not in merge_tutorial
    assert "from bioimageflow_common_tools import InnerJoin" in merge_tutorial
    assert "Merge strategies: :class:`~bioimageflow_common_tools.InnerJoin`" in architecture


def test_quickstart_examples_use_clean_tool_naming_and_wetlands_dependencies() -> None:
    docs = {
        "README.md": _text("README.md"),
        "docs/source/quickstart.rst": _text("docs/source/quickstart.rst"),
    }

    for text in docs.values():
        assert "\n    name =" not in text
        assert "\n       name =" not in text
        assert "display_name =" in text
        assert '"pip": ["imageio", "numpy"]' in text
        assert 'Workflow(storage_path="./bif_data", engine="wetlands")' in text


def test_graph_docs_do_not_claim_tool_name_attribute_sets_default_node_name() -> None:
    graph = _text("docs/source/concepts/graph.rst")

    assert "tool's ``name`` attribute" not in graph
    assert "tool class name" in graph
    assert "name=\"blur_fine\"" in graph


def test_parallelism_docs_do_not_claim_unsupported_parsl_round_trips() -> None:
    parallelism = _text("docs/source/tutorials/parallelism.rst")

    assert 'engine="parsl"' in parallelism
    assert "future Parsl-backed engine" in parallelism
    assert "round-trip" not in parallelism


def test_specs_document_clean_tool_identity_api() -> None:
    specs = _text("docs/source/specs.md")

    assert not re.search(r"^    name = \"", specs, flags=re.MULTILINE)
    assert "display_name = " in specs
    assert "class name and a counter" in specs
    assert "tool's `name` attribute" not in specs
    assert "`display_name`" in specs
    assert "Each tool runs in its own isolated Conda environment" not in specs


def test_specs_document_common_tools_merge_exports() -> None:
    specs = _text("docs/source/specs.md")

    assert "bioimageflow.merge" not in specs
    assert "merge.py        # Built-in merge DataFrameTools" not in specs
    assert "# Built-in merge tools" not in specs
    assert "Built-in merge tools" not in specs
    assert "from bioimageflow_common_tools import CrossJoin, JoinOnColumn" in specs
    assert "bioimageflow_common_tools" in specs


def test_readme_places_merge_tools_in_common_tools_package() -> None:
    readme = _text("README.md")

    assert "built-in inner join, cross join, concat, and collect operations" not in readme
    assert "Merge strategies         " not in readme
    assert "bioimageflow-common-tools" in readme


def test_parallelism_examples_use_wetlands_for_wetlands_knobs() -> None:
    parallelism = _text("docs/source/tutorials/parallelism.rst")

    assert 'Workflow(max_workers=4, engine="wetlands")' in parallelism
    assert 'Workflow(engine="wetlands")' in parallelism


def test_output_cache_reference_does_not_point_legacy_shape_at_current_specs() -> None:
    storage_reference = _text("docs/source/reference/output_cache_storage.md")

    assert "legacy storage shape documented in the exhaustive specification" not in storage_reference
    assert "older releases and historical documentation" in storage_reference


def test_tool_packaging_imports_common_tools_merge_exports() -> None:
    text = _text("docs/source/tutorials/tool_packaging.rst")

    assert "from bioimageflow import Workflow, Concat" not in text
    assert "from bioimageflow_common_tools import Concat" in text


def test_environment_reference_matches_local_v1_resource_semantics() -> None:
    text = _text("docs/source/reference/environments.rst")

    assert "raised at construction time" not in text
    assert "workflow containing those reachable tools is planned or computed" in text
    assert "max_concurrent`` clamps" not in text
    assert "Direct and Wetlands v1 do not enforce" in text


def test_caching_docs_surface_path_based_external_file_caveat() -> None:
    caching = _text("docs/source/concepts/caching.rst")
    quickstart = _text("docs/source/quickstart.rst")

    assert "External file references are path-based" in caching
    assert "modified in place without changing its path" in caching
    assert "input references, parameters, and tool versions" in quickstart


def test_tool_package_reference_documents_manual_publish_boundary() -> None:
    text = _text("docs/source/reference/tool_packages.md")

    assert "Publishing is currently manual and outside CI deployment" in text
    assert "does not upload them to an index" in text
