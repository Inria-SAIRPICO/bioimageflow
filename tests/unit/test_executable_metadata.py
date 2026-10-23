"""Operation-owned executable facts reuse process-fixed installation admission."""

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


def test_host_inventory_is_shared_across_operations_until_restart(monkeypatch):
    from bioimageflow import worker_origins

    scans = []
    versions = {"first-dist": "1", "second-dist": "2"}
    packages = {"first": ["first-dist"], "second": ["second-dist"]}
    held = None
    def inventory():
        nonlocal held
        if held is None:
            scans.append("inventory")
            providers = {name: object() for name in versions}
            held = {"providers": {name: [provider] for name, provider in providers.items()},
                    "imports": {name: [providers[distribution] for distribution in distributions]
                                for name, distributions in packages.items()},
                    "facts": {id(provider): (name, versions[name]) for name, provider in providers.items()}}
        return held
    monkeypatch.setattr(worker_origins, "_host_inventory", inventory)
    worker_origins._host_distribution.cache_clear()
    try:
        metadata = ExecutableMetadata()
        assert metadata.resolve("first", None) == ("first-dist", "1")
        assert metadata.resolve("second", None) == ("second-dist", "2")
        assert metadata.resolve("missing", None) is None
        assert scans == ["inventory"]
        versions["first-dist"] = "9"
        packages["missing"] = ["second-dist"]
        metadata.clear()
        assert metadata.resolve("first", None) == ("first-dist", "1")
        assert ExecutableMetadata().resolve("missing", None) is None
        assert scans == ["inventory"]
    finally:
        worker_origins._host_distribution.cache_clear()


def test_explicit_distribution_facts_use_fixed_inventory_without_discovery(monkeypatch):
    from bioimageflow import worker_origins

    provider = object()
    inventory = {"providers": {"same-dist": [provider]}, "facts": {id(provider): ("same-dist", "4")}}
    def forbidden(*args):
        raise AssertionError("Held explicit distribution facts need no metadata discovery")
    monkeypatch.setattr(worker_origins, "_host_inventory", lambda: inventory)
    monkeypatch.setattr(importlib.metadata, "version", forbidden)
    monkeypatch.setattr(importlib.metadata, "packages_distributions", forbidden)
    worker_origins._host_distribution.cache_clear()
    try:
        metadata = ExecutableMetadata()
        assert metadata.resolve("first", "same-dist") == ("same-dist", "4")
        metadata.clear()
        assert metadata.resolve("second", "same-dist") == ("same-dist", "4")
    finally:
        worker_origins._host_distribution.cache_clear()


def test_selected_root_admission_requires_explicit_invalidation_to_refresh(tool_store, monkeypatch, request):
    from bioimageflow import worker_origins
    from bioimageflow_core.import_context import admit_import_root, invalidate_import_root

    calls = []
    def admit(root, *, import_package):
        calls.append((root, import_package))
        return admit_import_root(root, import_package=import_package)
    monkeypatch.setattr(worker_origins, "admit_import_root", admit)
    metadata = ExecutableMetadata()
    root = str(tool_store / "dummy_tools" / "1.0.0")
    request.addfinalizer(lambda: invalidate_import_root(root, import_package="dummy_tools"))
    first = metadata.import_admission(root, "dummy_tools")
    assert metadata.import_admission(root, "dummy_tools") is first
    assert first.to_scientific_facts() == {"dependency_versions": {"dep-pkg": "1.0.0"}}
    assert len(calls) == 1
    dependency_metadata = tool_store / "dummy_tools" / "1.0.0" / "dep_pkg-1.0.0.dist-info" / "METADATA"
    dependency_metadata.write_text(dependency_metadata.read_text().replace("Version: 1.0.0", "Version: 9.0.0"))
    assert metadata.import_admission(root, "dummy_tools") is first
    metadata.clear()
    refreshed = metadata.import_admission(root, "dummy_tools")
    assert refreshed is first
    assert refreshed.to_scientific_facts() == {"dependency_versions": {"dep-pkg": "1.0.0"}}
    assert len(calls) == 2
    invalidate_import_root(root, import_package="dummy_tools")
    metadata.clear()
    replacement = metadata.import_admission(root, "dummy_tools")
    assert replacement is not first
    assert replacement.to_scientific_facts() == {"dependency_versions": {"dep-pkg": "9.0.0"}}
    assert len(calls) == 3
