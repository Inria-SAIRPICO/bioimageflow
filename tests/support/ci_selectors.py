"""Shared pytest selectors used by CI contract tests and local helpers."""

FAST_TEST_SELECTOR = (
    "not slow and not acceptance and not packaging and not package_tools and not complete "
    "and not wetlands and not public_data and not external_binary and not sairpico_binary "
    "and not model_runtime"
)
FAST_TEST_COMMAND = f'uv run pytest tests -m "{FAST_TEST_SELECTOR}"'
UNIT_TEST_COMMAND = f'uv run pytest tests/unit -m "{FAST_TEST_SELECTOR}"'
DIRECT_INTEGRATION_TEST_COMMAND = (
    f'uv run pytest tests/integration -m "{FAST_TEST_SELECTOR}"'
)
FAST_TEST_WITHOUT_SHARED_MEMORY_COMMAND = (
    f'uv run pytest tests -m "{FAST_TEST_SELECTOR} and not shared_memory"'
)
PYTHON_COMPAT_TEST_SELECTOR = f"compat and {FAST_TEST_SELECTOR}"
PYTHON_COMPAT_TEST_COMMAND = f'uv run pytest tests -m "{PYTHON_COMPAT_TEST_SELECTOR}"'

ACCEPTANCE_TEST_SELECTOR = "acceptance and not complete"
ACCEPTANCE_TEST_COMMAND = f'uv run pytest -m "{ACCEPTANCE_TEST_SELECTOR}"'

PACKAGE_TOOLS_TEST_SELECTOR = "package_tools and not complete"
PACKAGE_TOOLS_TEST_COMMAND = f'uv run pytest -m "{PACKAGE_TOOLS_TEST_SELECTOR}"'

PACKAGE_ARTIFACTS_COMMAND = "uv run pytest tests/unit/test_package_artifacts.py"
CI_PACKAGE_ARTIFACTS_COMMAND = (
    "BIOIMAGEFLOW_PACKAGE_ARTIFACTS_DIR=dist/packages "
    "uv run pytest tests/unit/test_package_artifacts.py"
)
PACKAGE_METADATA_CONTRACTS_COMMAND = (
    "uv run pytest tests/unit/test_package_artifacts.py "
    "tests/unit/test_package_docs_dependency_posture.py "
    "tests/unit/test_core_package_metadata.py"
)
CI_QUALITY_CONFIG_COMMAND = (
    "uv run pytest tests/unit/test_development_workflow.py "
    "tests/unit/test_complete_test_gating.py tests/unit/test_release_tooling.py"
)
FILE_SIZE_COMMAND = "uv run python scripts/check_file_sizes.py"
IMPORT_BOUNDARY_COMMAND = "uv run python scripts/check_import_boundaries.py"
DOCS_BUILD_COMMAND = "uv run sphinx-build -W --keep-going docs/source docs/_build/html"
RUFF_COMMAND = "uv run ruff check ."
PYRIGHT_COMMAND = "uv run pyright"
PACKAGE_BUILD_COMMAND = "uv build --all-packages --out-dir dist/packages"
DEFAULT_TEST_COMMAND = "uv run pytest"
