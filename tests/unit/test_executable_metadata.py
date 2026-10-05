"""Operation-local distribution admission shares discovery without stale versions."""

import importlib.metadata

from bioimageflow.worker_origins import ExecutableMetadata


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
