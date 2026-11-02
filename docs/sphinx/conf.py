"""Sphinx configuration for scitex-agent-container documentation."""

# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

import os
import sys

sys.path.insert(0, os.path.abspath("../../src"))

# -- Project information -----------------------------------------------------

project = "scitex-agent-container"
copyright = "2026, Yusuke Watanabe"
author = "Yusuke Watanabe"

# Dynamic version from pyproject.toml
try:
    from importlib.metadata import version as _get_version

    release = _get_version("scitex-agent-container")
except Exception:
    release = "0.0.0"

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.coverage",
    "sphinx_rtd_theme",
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_autodoc_typehints",
]

# Autodoc settings
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__",
}

# Mock imports for packages that may not be installed on RTD
autodoc_mock_imports = [
    "fastmcp",
]

# Autosummary settings
autosummary_generate = True

# Napoleon settings for numpy/google style docstrings
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = True
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "to_claude/**"]

# The suffix(es) of source filenames.
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"

html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "includehidden": True,
    "titles_only": False,
    "prev_next_buttons_location": "bottom",
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ["_static"]

# RTD-specific configurations
html_title = f"{project} v{release}"
html_short_title = project
html_logo = None  # Add path to logo if available
html_favicon = None  # Add path to favicon if available

# Show "Edit on GitHub" links
html_context = {
    "display_github": True,
    "github_user": "ywatanabe1989",
    "github_repo": "scitex-agent-container",
    "github_version": "main",
    "conf_py_path": "/docs/sphinx/",
}

# MyST parser configuration
myst_enable_extensions = [
    "dollarmath",
    "colon_fence",
    "deflist",
    "html_admonition",
    "html_image",
    "replacements",
    "smartquotes",
    "substitution",
    "tasklist",
]
# Auto-generate anchors for headings up to h3 so cross-document
# fragment links (e.g. ``[X](spec-reference.md#kind-agentproxy)``)
# resolve without explicit ``(target)=`` labels in the source.
myst_heading_anchors = 3

# -- Options for intersphinx extension ---------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}



def _capture_guide_source(app, relative_path, parent_docname, content):
    """Retain the actual include bytes consumed by the MyST parser."""
    from pathlib import Path

    canonical = Path(app.srcdir).parent / "talking-to-agents.md"
    included = Path(app.srcdir) / relative_path
    if parent_docname == "talking-to-agents" and included.resolve() == canonical.resolve():
        app.env.metadata.setdefault(parent_docname, {})["sac_guide_source"] = content[0]


def _guide_source_fingerprint(app, pagename, templatename, context, doctree):
    """Bind packaged guide HTML to the exact source consumed by Sphinx."""
    if pagename != "talking-to-agents":
        return
    import hashlib

    source = app.env.metadata[pagename]["sac_guide_source"].encode("utf-8")
    digest = hashlib.sha256(source).hexdigest()
    context["metatags"] += (
        f'<meta name="sac-guide-source-sha256" content="{digest}" />'
    )


def _export_guide_source(app, exception):
    """Expose the consumed canonical source rather than its thin include wrapper."""
    if exception is not None or app.builder.format != "html":
        return
    from pathlib import Path

    source = app.env.metadata["talking-to-agents"]["sac_guide_source"].encode("utf-8")
    target = Path(app.outdir) / "_sources/talking-to-agents.md.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)


def setup(app):
    app.connect("include-read", _capture_guide_source)
    app.connect("html-page-context", _guide_source_fingerprint)
    app.connect("build-finished", _export_guide_source)
