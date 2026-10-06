"""Package documentation uses declared metadata rather than checkout names."""

from pathlib import Path

import pytest

from docs import generate_tool_package_docs as generator
from docs.generate_tool_package_docs import build_pages, docs_from_package_dir


def test_package_docs_parse_full_toml_and_use_declared_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "checkout-label"
    docs = package / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text("# Local index\n")
    (package / "pyproject.toml").write_text(
        "[project]\nname = 'declared-tools'\nversion = '1.0.0'\n"
        "[tool.bioimageflow.docs]\ninclude_in_main_docs = true\n"
        "title = 'A # title with = punctuation'\n"
    )

    metadata = docs_from_package_dir(package, first_party=False)

    assert metadata is not None
    assert metadata.name == "declared-tools"
    assert metadata.slug == "declared-tools"
    assert metadata.title == "A # title with = punctuation"
    assert metadata.package_dir == package
    assert metadata.index == docs / "index.md"
    pages = build_pages([metadata])
    assert any("declared-tools" in path.parts for path in pages)
    assert any(metadata.title in content for content in pages.values())
    monkeypatch.setattr(generator, "PACKAGES_DIR", tmp_path)
    monkeypatch.setattr(generator, "FIRST_PARTY_ORDER", ["checkout-label"])
    monkeypatch.delenv("BIOIMAGEFLOW_DOC_PACKAGE_PATHS", raising=False)
    alias = tmp_path / "another-checkout"
    (alias / "docs").mkdir(parents=True)
    (alias / "docs/index.md").write_text("# Alias\n")
    (alias / "pyproject.toml").write_text(
        "[project]\nname = 'Declared_Tools'\nversion = '1.0.0'\n"
    )
    assert generator.discover_package_docs() == [metadata]
    assert docs_from_package_dir(alias, first_party=True).name == "declared-tools"
    assert docs_from_package_dir(tmp_path / "no-project", first_party=True) is None
    for text in ("[project]\nversion = '1.0.0'\n", "[project]\nname = 'not a name!'\n"):
        (alias / "pyproject.toml").write_text(text)
        with pytest.raises((TypeError, ValueError)):
            docs_from_package_dir(alias, first_party=True)
