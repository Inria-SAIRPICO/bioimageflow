"""Operation-local distribution admission shares discovery without stale versions."""

import importlib.metadata
import hashlib
import json
from pathlib import Path

import pytest

from bioimageflow.worker_origins import ExecutableMetadata, capture_tool_executable
from bioimageflow_core import Arguments, IOModel, ProcessingTool, RowConsumption

pytest_plugins = ("tests.testkit.tool_loader",)


@pytest.mark.parametrize("source_bound", [False, True])
def test_direct_delegate_identity_preserves_selected_batch_dispatch(source_bound):
    def factory(value):
        class Tool(ProcessingTool):
            row_consumption = RowConsumption.MAPPED

            class Inputs(IOModel):
                pass

            class Outputs(IOModel):
                value: int

            def process_row(self, arguments):
                return self.Outputs(value=value)

            def process_batch(self, arguments_list):
                return [self.process_row(arguments) for arguments in arguments_list]

        if source_bound:
            Tool._bif_custom_source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return Tool()

    one, nine = factory(1), factory(9)
    first = capture_tool_executable(one, managed=False, canonicalize=json.dumps)
    second = capture_tool_executable(nine, managed=False, canonicalize=json.dumps)
    assert first.scientific_key["runtime_digest"] != second.scientific_key["runtime_digest"]
    assert tuple(first.callbacks) == ("process_batch",)
    assert first.callbacks["process_batch"].__self__ is one
    assert second.callbacks["process_batch"].__self__ is nine
    assert [output.value for output in first.callbacks["process_batch"]([Arguments()])] == [1]
    assert [output.value for output in second.callbacks["process_batch"]([Arguments()])] == [9]


def test_package_map_shared_across_roots_and_refreshed_next_operation(monkeypatch):
    calls = []
    versions = {"first-dist": "1", "second-dist": "2"}
    packages = {"first": ["first-dist"], "second": ["second-dist"]}

    def package_map():
        calls.append("map")
        return dict(packages)

    monkeypatch.setattr(importlib.metadata, "packages_distributions", package_map)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: versions[name])
    metadata = ExecutableMetadata()
    assert metadata.resolve("first", None) == ("first-dist", "1")
    assert metadata.resolve("second", None) == ("second-dist", "2")
    assert metadata.resolve("missing", None) is None
    assert metadata.resolve("first", None) == ("first-dist", "1")
    assert calls == ["map"]
    versions["first-dist"] = "9"
    packages["missing"] = ["second-dist"]
    assert metadata.resolve("first", None) == ("first-dist", "1")
    assert metadata.resolve("missing", None) is None
    metadata.clear()
    assert metadata.resolve("first", None) == ("first-dist", "9")
    assert metadata.resolve("missing", None) == ("second-dist", "2")
    assert calls == ["map", "map"]


def test_explicit_distribution_facts_do_not_use_package_discovery(monkeypatch):
    calls = []

    def version(name):
        calls.append(name)
        return "4"

    def forbidden():
        raise AssertionError("explicit distribution needs no package map")

    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setattr(importlib.metadata, "packages_distributions", forbidden)
    metadata = ExecutableMetadata()
    assert metadata.resolve("first", "same-dist") == ("same-dist", "4")
    assert metadata.resolve("second", "same-dist") == ("same-dist", "4")
    assert calls == ["same-dist"]


def test_selected_root_admission_shared_and_refreshed_next_operation(tool_store, monkeypatch):
    from bioimageflow import worker_origins
    from bioimageflow_core.import_context import admit_import_root

    calls = []
    def admit(root, *, import_package):
        calls.append((root, import_package))
        return admit_import_root(root, import_package=import_package)
    monkeypatch.setattr(worker_origins, "admit_import_root", admit)
    metadata = ExecutableMetadata()
    root = str(tool_store / "dummy_tools" / "1.0.0")
    first = metadata.import_admission(root, "dummy_tools")
    assert metadata.import_admission(root, "dummy_tools") is first
    assert first.to_scientific_facts() == {"dependency_versions": {"dep-pkg": "1.0.0"}}
    assert len(calls) == 1
    dependency_metadata = tool_store / "dummy_tools" / "1.0.0" / "dep_pkg-1.0.0.dist-info" / "METADATA"
    dependency_metadata.write_text(dependency_metadata.read_text().replace("Version: 1.0.0", "Version: 9.0.0"))
    assert metadata.import_admission(root, "dummy_tools") is first
    metadata.clear()
    refreshed = metadata.import_admission(root, "dummy_tools")
    assert refreshed.to_scientific_facts() == {"dependency_versions": {"dep-pkg": "9.0.0"}}
    assert len(calls) == 2
