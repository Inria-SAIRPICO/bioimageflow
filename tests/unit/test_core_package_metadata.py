"""Package metadata contract tests for bioimageflow-core."""

from __future__ import annotations

import ast
import re
import runpy
from pathlib import Path
import sys

from packaging.requirements import Requirement
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).parents[2]
FIRST_PARTY_DISTRIBUTIONS = {
    "bioimageflow",
    "bioimageflow-common-tools",
    "bioimageflow-core",
    "bioimageflow-io-tools",
    "bioimageflow-measurement-tools",
    "bioimageflow-restoration-tools",
    "bioimageflow-sairpico-tools",
    "bioimageflow-segmentation-tools",
    "bioimageflow-spot-tools",
    "bioimageflow-tracking-tools",
}
EXPECTED_PROJECT_URLS = {
    "Homepage": "https://github.com/Inria-SAIRPICO/bioimageflow",
    "Documentation": "https://bioimageflow.readthedocs.io/latest/",
    "Repository": "https://github.com/Inria-SAIRPICO/bioimageflow",
    "Issues": "https://github.com/Inria-SAIRPICO/bioimageflow/issues",
}
BASE_CLASSIFIERS = {
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Science/Research",
    "License :: OSI Approved :: BSD License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Scientific/Engineering :: Image Processing",
}
HEAVY_OR_DEFERRED_DEPENDENCY_NAMES = {
    "big-fish",
    "btrack",
    "cellpose",
    "laptrack",
    "parsl",
    "simpleitk",
    "stardist",
    "tensorflow",
}


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _dependency_names(dependencies: list[str]) -> set[str]:
    names = set()
    for dependency in dependencies:
        names.add(_dependency_name(dependency))
    return names


def _dependency_name(dependency: str) -> str:
    name = dependency.split(";", 1)[0].strip()
    for separator in ("<", ">", "=", "!", "~", "["):
        name = name.split(separator, 1)[0].strip()
    return name.lower()


def _package_pyprojects() -> list[Path]:
    return sorted((ROOT / "packages").glob("*/pyproject.toml"))


def _project(path: Path) -> dict:
    return _pyproject(path)["project"]


def _pytest_marker_names_from_pyproject() -> set[str]:
    markers = _pyproject(ROOT / "pyproject.toml")["tool"]["pytest"]["ini_options"]["markers"]
    return {marker.split(":", 1)[0] for marker in markers}


def _dependency_entries(dependencies: list[str], package_name: str) -> list[str]:
    return [
        dependency
        for dependency in dependencies
        if _dependency_name(dependency) == package_name.lower()
    ]


def _runtime_imports_package(package_dir: Path, import_name: str) -> bool:
    package_names = [
        child.name
        for child in package_dir.iterdir()
        if child.is_dir() and (child / "__init__.py").exists()
    ]
    for package_name in package_names:
        for source in sorted((package_dir / package_name).glob("**/*.py")):
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == import_name for alias in node.names):
                        return True
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    if node.module == import_name or node.module.startswith(f"{import_name}."):
                        return True
    return False


def test_direct_runtime_imports_are_declared() -> None:
    expected = {
        "bioimageflow": {"numpy"},
        "bioimageflow-spot-tools": {"pandas"},
        "bioimageflow-tracking-tools": {"pandas"},
    }
    offenders: dict[str, set[str]] = {}

    for path in _package_pyprojects():
        project = _project(path)
        dependencies = _dependency_names(project.get("dependencies", []))
        missing = expected.get(project["name"], set()) - dependencies
        if missing:
            offenders[str(path.relative_to(ROOT))] = missing

    assert offenders == {}


def test_core_declares_numpy_runtime_dependency() -> None:
    pyproject = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    dependencies = _dependency_names(pyproject["project"]["dependencies"])

    assert "numpy" in dependencies
    assert "zero dependencies" not in pyproject["project"]["description"].lower()


def test_workspace_core_requirement_matches_local_core_version() -> None:
    workspace = _pyproject(ROOT / "pyproject.toml")
    core = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    [dependency] = workspace["project"]["dependencies"]
    _assert_bounded_local_dependency(
        dependency,
        "bioimageflow-core",
        core["project"]["version"],
    )


def test_first_party_package_versions_are_valid_stable_release_versions() -> None:
    offenders = {}

    for path in _package_pyprojects():
        version = Version(_project(path)["version"])
        if (
            len(version.release) != 3
            or version.is_prerelease
            or version.is_devrelease
            or version.is_postrelease
        ):
            offenders[str(path.relative_to(ROOT))] = str(version)

    assert offenders == {}


def test_first_party_packages_target_supported_python_floors() -> None:
    requires_python = {
        str(path.relative_to(ROOT)): _project(path)["requires-python"]
        for path in _package_pyprojects()
    }

    expected = {path: ">=3.10" for path in requires_python}
    expected["packages/bioimageflow-core/pyproject.toml"] = ">=3.9"

    assert requires_python == expected


def test_core_declares_python39_worker_runtime_classifier() -> None:
    core = _project(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    assert "Programming Language :: Python :: 3.9" in core["classifiers"]


def _assert_bounded_local_dependency(dependency: str, package_name: str, version: str) -> None:
    requirement = Requirement(dependency)

    assert requirement.name == package_name
    assert Version(version) in requirement.specifier
    assert any(specifier.operator in {"<", "<="} for specifier in requirement.specifier)


def test_first_party_packages_declare_bounded_compatible_core_versions() -> None:
    core = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")
    offenders: dict[str, list[str]] = {}

    for path in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        project = _pyproject(path)["project"]
        dependencies = project.get("dependencies", [])
        core_dependencies = [
            dependency for dependency in dependencies
            if dependency.startswith("bioimageflow-core")
        ]
        if project["name"] == "bioimageflow-core":
            continue
        try:
            [dependency] = core_dependencies
            _assert_bounded_local_dependency(
                dependency,
                "bioimageflow-core",
                core["project"]["version"],
            )
        except (AssertionError, ValueError):
            offenders[str(path.relative_to(ROOT))] = core_dependencies

    assert offenders == {}


def test_tool_packages_declare_bounded_compatible_orchestrator_versions() -> None:
    orchestrator = _project(ROOT / "packages" / "bioimageflow" / "pyproject.toml")
    offenders: dict[str, list[str]] = {}

    for path in _package_pyprojects():
        project = _project(path)
        dependencies = project.get("dependencies", [])
        orchestrator_dependencies = _dependency_entries(dependencies, "bioimageflow")
        if project["name"] in {"bioimageflow", "bioimageflow-core"}:
            continue
        if not _runtime_imports_package(path.parent, "bioimageflow"):
            continue
        try:
            [dependency] = orchestrator_dependencies
            _assert_bounded_local_dependency(
                dependency,
                "bioimageflow",
                orchestrator["version"],
            )
        except (AssertionError, ValueError):
            offenders[str(path.relative_to(ROOT))] = orchestrator_dependencies

    assert offenders == {}


def test_publishable_packages_declare_existing_readmes() -> None:
    offenders: dict[str, str | None] = {}

    for path in _package_pyprojects():
        project = _project(path)
        readme = project.get("readme")
        if not isinstance(readme, str) or not (path.parent / readme).is_file():
            offenders[str(path.relative_to(ROOT))] = readme

    assert offenders == {}


def test_repository_declares_bsd_4_clause_license() -> None:
    license_path = ROOT / "LICENSE"

    assert license_path.is_file()
    assert "BSD 4-Clause License" in license_path.read_text()


def test_publishable_packages_declare_release_metadata() -> None:
    offenders: dict[str, list[str]] = {}

    for path in _package_pyprojects():
        project = _project(path)
        missing = []
        if project.get("authors") != [{"name": "BioImageFlow Contributors"}]:
            missing.append("authors")
        if project.get("license") != "BSD-4-Clause":
            missing.append("license")
        if project.get("license-files") != ["LICENSE"]:
            missing.append("license-files")
        if project.get("urls") != EXPECTED_PROJECT_URLS:
            missing.append("urls")
        if not BASE_CLASSIFIERS.issubset(set(project.get("classifiers", []))):
            missing.append("classifiers")
        keywords = set(project.get("keywords", []))
        if not {"bioimageflow", "bioimage-analysis", "workflow"}.issubset(keywords):
            missing.append("keywords")
        if missing:
            offenders[str(path.relative_to(ROOT))] = missing

    assert offenders == {}


def test_top_level_runtime_packages_define_explicit_public_exports() -> None:
    import bioimageflow
    import bioimageflow_core

    for module in [bioimageflow, bioimageflow_core]:
        exports = getattr(module, "__all__", None)
        assert isinstance(exports, list)
        assert exports == sorted(exports)
        assert len(exports) == len(set(exports))
        assert all(hasattr(module, name) for name in exports)


def test_publishable_package_readme_titles_include_distribution_name() -> None:
    offenders: dict[str, str] = {}

    for path in _package_pyprojects():
        project = _project(path)
        title = (path.parent / project["readme"]).read_text().splitlines()[0]
        if project["name"] not in title:
            offenders[str(path.relative_to(ROOT))] = title

    assert offenders == {}


def test_only_orchestrator_declares_bounded_parsl_extra() -> None:
    extras = {
        str(path.relative_to(ROOT)): _pyproject(path)["project"].get(
            "optional-dependencies",
            {},
        )
        for path in _package_pyprojects()
    }

    assert extras.pop("packages/bioimageflow/pyproject.toml") == {
        "parsl": ["parsl>=2026.5.25,<2026.6"],
        "psij": ["psij-python>=0.9.11,<0.10"],
    }
    assert extras and all(value == {} for value in extras.values())


def test_package_uv_sources_match_first_party_runtime_dependencies() -> None:
    offenders: dict[str, dict[str, set[str]]] = {}

    for path in _package_pyprojects():
        pyproject = _pyproject(path)
        project = pyproject["project"]
        dependencies = {
            _dependency_name(dependency)
            for dependency in project.get("dependencies", [])
            if _dependency_name(dependency) in FIRST_PARTY_DISTRIBUTIONS
        }
        sources = set(pyproject.get("tool", {}).get("uv", {}).get("sources", {}))
        if dependencies != sources:
            offenders[str(path.relative_to(ROOT))] = {
                "dependencies": dependencies,
                "sources": sources,
            }

    assert offenders == {}


def test_root_dependency_groups_only_select_bounded_parsl_optional_runtime() -> None:
    groups = _pyproject(ROOT / "pyproject.toml").get("dependency-groups", {})
    offenders: dict[str, list[str]] = {}

    for group_name, dependencies in groups.items():
        heavy = sorted(
            dependency
            for dependency in dependencies
            if (
                _dependency_name(dependency) in HEAVY_OR_DEFERRED_DEPENDENCY_NAMES
                and _dependency_name(dependency) != "parsl"
            )
        )
        if heavy:
            offenders[group_name] = heavy

    assert offenders == {}
    assert _dependency_entries(groups["dev"], "parsl") == [
        "parsl>=2026.5.25,<2026.6"
    ]


def test_pytest_marker_registry_matches_pyproject() -> None:
    namespace: dict[str, object] = {}
    exec((ROOT / "conftest.py").read_text(), namespace)
    registered_marker_entries = namespace["REGISTERED_TEST_MARKERS"]
    complete_marker_entries = namespace["COMPLETE_TEST_MARKERS"]
    assert isinstance(registered_marker_entries, list)
    assert isinstance(complete_marker_entries, list)

    registered_markers = {
        marker.split(":", 1)[0]
        for marker in registered_marker_entries
        if isinstance(marker, str)
    }
    external_markers = {
        marker.split(":", 1)[0]
        for marker in complete_marker_entries
        if isinstance(marker, str)
    }

    assert registered_markers == _pytest_marker_names_from_pyproject()
    assert external_markers < registered_markers


def test_docs_do_not_contain_placeholder_repository_urls() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "docs" / "source" / "installation.rst",
        ROOT / "docs" / "source" / "conf.py",
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in docs
        if "github.com/your-org/bioimageflow" in path.read_text()
    ]

    assert offenders == []


def test_readme_quick_start_declares_imported_processing_dependencies() -> None:
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
        "docs/source/index.rst": (ROOT / "docs" / "source" / "index.rst").read_text(),
    }

    for text in docs.values():
        assert "environment = GENERAL_ENV" in text
        assert "Workflow(" in text
        assert "storage_path=" in text
        assert "from skimage.io import imread, imsave" not in text
        assert re.search(r"import imageio\.v3 as iio", text) is not None


def test_sphinx_quickstart_declares_imported_processing_dependencies() -> None:
    quickstart = (ROOT / "docs" / "source" / "quickstart.rst").read_text()

    assert "environment = GENERAL_ENV" in quickstart
    assert 'Workflow(storage_path="./results")' in quickstart
    assert "from skimage.io import imread, imsave" not in quickstart
    assert re.search(r"import imageio\.v3 as iio", quickstart) is not None


def test_docs_python_requirement_matches_supported_contract() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "docs" / "source" / "installation.rst",
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in docs
        if "Python >= 3.13" in path.read_text() or "Python >= 3.10" not in path.read_text()
    ]

    assert offenders == []


def test_docs_release_matches_orchestrator_version() -> None:
    orchestrator = _project(ROOT / "packages" / "bioimageflow" / "pyproject.toml")
    conf = ROOT / "docs" / "source" / "conf.py"

    assert runpy.run_path(str(conf))["release"] == orchestrator["version"]


def test_primary_docs_do_not_describe_core_as_zero_dependency() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "docs" / "source" / "index.rst",
        ROOT / "docs" / "source" / "installation.rst",
        ROOT / "docs" / "source" / "concepts" / "architecture.rst",
        ROOT / "docs" / "source" / "reference" / "api" / "core.rst",
        ROOT / "docs" / "source" / "specs.md",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "io.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "shm.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "tool.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "types.py",
    ]

    offenders = [
        str(path.relative_to(ROOT))
        for path in docs
        if "zero-dependency core" in path.read_text().lower()
        or "zero external dependencies" in path.read_text().lower()
        or "zero deps" in path.read_text().lower()
    ]

    assert offenders == []
