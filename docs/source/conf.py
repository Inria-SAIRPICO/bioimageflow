# Configuration file for the Sphinx documentation builder.

from importlib.metadata import version as distribution_version
from pathlib import Path
import sys

# Add both packages to sys.path for autodoc
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "packages" / "bioimageflow-core"))
sys.path.insert(0, str(root / "packages" / "bioimageflow"))

# -- Project information -----------------------------------------------------

project = "BioImageFlow"
copyright = "2026, BioImageFlow Contributors"
author = "BioImageFlow Contributors"
release = distribution_version("bioimageflow")

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_autodoc_typehints",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

myst_heading_anchors = 4

templates_path = ["_templates"]
exclude_patterns = []

language = "en"

# -- Options for HTML output -------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_title = "BioImageFlow"

html_theme_options = {
    "source_repository": "https://github.com/bioimageit/bioimageflow",
    "source_branch": "main",
    "source_directory": "docs/source/",
    "source_view_link": "https://github.com/bioimageit/bioimageflow/blob/main/docs/source/{filename}",
    "source_edit_link": "https://github.com/bioimageit/bioimageflow/edit/main/docs/source/{filename}",
}

# -- Autodoc options ---------------------------------------------------------

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_class_signature = "separated"

# -- Napoleon options --------------------------------------------------------

napoleon_google_docstrings = True
napoleon_numpy_docstrings = True
napoleon_use_ivar = True

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
}


def setup(app):
    app.add_js_file("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js")
    app.add_js_file(None, body="mermaid.initialize({startOnLoad: true});")
